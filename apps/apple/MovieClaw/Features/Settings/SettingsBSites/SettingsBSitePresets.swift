import SwiftUI

/// 「搜索分类」页签的数据源（对应 Web `lib/search-prefs.tsx` 的保存链路）。
///
/// 偏好是一整个有序列表（内置分类与自定义分类混排），后端整体覆盖式保存：
/// - 所有改动（显隐、排序、增删改预设）都走 `apply`：先乐观更新，再 `PUT /search/presets`，
///   成功以后端规范化后的列表回写，失败回滚并返回中文原因；
/// - 拉取失败不致命：沿用内置默认（与后端默认一致，同 Web 兜底）。
@Observable
final class SettingsBSitePresetStore {
    private(set) var tabs: [SettingsBSitePresetTab] = SettingsBSitePresetStore.defaults
    private(set) var loading = true
    private(set) var busy = false
    var error: String?

    /// 默认标签：常用四类可见，其余隐藏，无预设（同 Web DEFAULT_SEARCH_TABS）
    static let defaults: [SettingsBSitePresetTab] = SettingsBSitePresetCategory.options.enumerated().map { i, opt in
        SettingsBSitePresetTab(type: "category", id: opt.value, visible: i < 4)
    }

    func load(_ api: APIClient) async {
        if let view = try? await api.searchPresetsList() {
            tabs = view.presets.compactMap(SettingsBSitePresetTab.init(json:))
        }
        loading = false
    }

    /// 整体保存；返回 nil 表示成功，否则为错误文案（调用方决定展示位置）
    @discardableResult
    func apply(_ api: APIClient, _ next: [SettingsBSitePresetTab]) async -> String? {
        let previous = tabs
        tabs = next
        busy = true
        error = nil
        defer { busy = false }
        do {
            let view = try await api.searchPresetsUpdate(body: API.SearchPresetUpdate(presets: next.map(\.json)))
            tabs = view.presets.compactMap(SettingsBSitePresetTab.init(json:))
            return nil
        } catch {
            tabs = previous
            let message = error.localizedDescription.isEmpty ? "保存失败，请检查网络后重试" : error.localizedDescription
            self.error = message
            return message
        }
    }

    /// 自定义分类 id：前端生成短随机 id（偏好整体覆盖式保存，后端无法区分新旧行），同 Web `p-${nanoid(8)}`
    static func newPresetId() -> String {
        let alphabet = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        return "p-" + String((0..<8).map { _ in alphabet.randomElement()! })
    }
}

/// 搜索分类页签（对应 Web `SearchSection`）：
/// 一个统一混排的标签列表——内置分类（不可删只可隐藏）与自定义分类（可增删改）同列排序、同款显隐开关；
/// 「全部」固定在搜索面板首位，不在此列表中。所有改动即时保存到服务端，无需保存按钮。
///
/// 与 Web 的交互差异：Web 按住手柄拖动排序；手机上用系统列表的「调整顺序」编辑模式拖动（右侧把手），
/// 行内的「编辑 / 删除」两个按钮收进 ⋯ 菜单（窄屏放不下名称 + 两个按钮 + 开关）。
struct SettingsBSitePresetSection: View {
    let store: SettingsBSitePresetStore
    @Binding var editor: SettingsBSitePresetDraft?
    @Binding var editMode: EditMode

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    var body: some View {
        Section {
            ForEach(store.tabs, id: \.key) { tab in
                row(tab)
            }
            .onMove { from, to in
                var next = store.tabs
                next.move(fromOffsets: from, toOffset: to)
                Task { await store.apply(api, next) }
            }
            if let error = store.error {
                SettingsBNotice(text: error, tone: .danger)
                    .accessibilityIdentifier("preset-error")
            }
        } header: {
            HStack {
                Text("搜索分类")
                Spacer()
                Button(editMode.isEditing ? "完成" : "调整顺序") {
                    withAnimation { editMode = editMode.isEditing ? .inactive : .active }
                }
                .font(.footnote.weight(.medium))
                .textCase(nil)
                .disabled(store.loading)
                .accessibilityIdentifier("preset-reorder")
            }
        } footer: {
            Text("点「调整顺序」后拖动右侧把手即可排序——列表顺序即搜索面板中分类标签的排列顺序，「全部」固定在首位。自定义分类可组合多个资源分类与指定站点，一次搜索只打勾选的站点。改动即时保存到服务端，所有设备与浏览器保持一致。")
        }
    }

    private func row(_ tab: SettingsBSitePresetTab) -> some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(tab.label).font(.body.weight(.medium)).lineLimit(1)
                    if tab.isPreset { SettingsBBadge(text: "自定义", tone: .accent) }
                }
                if tab.isPreset {
                    Text(tab.summary).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                } else if let hint = SettingsBSitePresetCategory.hint(tab.id) {
                    Text(hint).font(.caption).foregroundStyle(Theme.textFaint).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 6)
            if tab.isPreset && !editMode.isEditing {
                Menu {
                    Button("编辑", systemImage: "pencil") { editor = SettingsBSitePresetDraft(tab) }
                    Button("删除", systemImage: "trash", role: .destructive) { Task { await delete(tab) } }
                } label: {
                    Image(systemName: "ellipsis").frame(width: 28, height: 28).contentShape(.rect)
                }
                .buttonStyle(.borderless)
                .disabled(store.busy || store.loading)
                .accessibilityLabel("「\(tab.label)」的操作")
                .accessibilityIdentifier("preset-menu-\(tab.id)")
            }
            Toggle(isOn: Binding(
                get: { tab.visible },
                set: { visible in
                    var next = store.tabs
                    guard let i = next.firstIndex(where: { $0.key == tab.key }) else { return }
                    next[i].visible = visible
                    Task { await store.apply(api, next) }
                }
            )) {
                Text(tab.label)
            }
            .labelsHidden()
            .disabled(store.busy || store.loading)
            .accessibilityLabel("在搜索分类中\(tab.visible ? "隐藏" : "展示")「\(tab.label)」")
            .accessibilityIdentifier("preset-visible-\(tab.id)")
        }
        .opacity(store.loading ? 0.5 : 1)
        .accessibilityIdentifier("preset-row-\(tab.id)")
    }

    private func delete(_ tab: SettingsBSitePresetTab) async {
        guard await feedback.confirm("删除自定义分类「\(tab.name)」？", message: "搜索历史不受影响。", confirmTitle: "删除", destructive: true) else { return }
        await store.apply(api, store.tabs.filter { $0.key != tab.key })
    }
}

/// 预设编辑器草稿：新建（editingId = nil）或编辑某个预设
struct SettingsBSitePresetDraft: Identifiable {
    let id = UUID()
    var editingId: String?
    var name = ""
    var categories: [String] = []
    var siteIds: [String] = []
    var posterMode = false
    var skipHistory = false

    init() {}

    init(_ tab: SettingsBSitePresetTab) {
        editingId = tab.id
        name = tab.name
        categories = tab.categories
        siteIds = tab.siteIds
        posterMode = tab.posterMode
        skipHistory = tab.skipHistory
    }
}

/// 预设编辑器（对应 Web `PresetEditor`）：名称 + 资源分类勾选 + 站点勾选 + 图览模式 + 无痕搜索。
///
/// 站点选项在打开时向后端拉取（已接入站点 ∩ 站点目录，取展示名与可用状态）；暂时不可用的照样可勾选，
/// 搜索时会自动跳过。分类/站点都不勾选表示「不限」，用芯片的空选态自然表达，不设「全选」。
/// 保存走与列表同一条链路（整体覆盖式保存）：失败时编辑器保持打开并就地显示原因。
struct SettingsBSitePresetEditorSheet: View {
    @State var draft: SettingsBSitePresetDraft
    let store: SettingsBSitePresetStore

    /// 站点勾选器选项：nil = 加载中；[] = 没有任何已接入站点
    private struct SiteOption { let siteId: String; let name: String; let usable: Bool }

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss
    @State private var siteOptions: [SiteOption]?
    @State private var error: String?

    var body: some View {
        SubsSheetScaffold(
            title: draft.editingId == nil ? "新建自定义分类" : "编辑自定义分类",
            confirm: SubsSheetConfirm(
                title: "保存",
                enabled: !draft.name.trimmingCharacters(in: .whitespaces).isEmpty,
                busy: store.busy,
                identifier: "preset-save"
            ) { Task { await save() } },
            ready: siteOptions != nil
        ) {
            if let error {
                Section { SubsNoticeRow(text: error, tone: .error) }
            }
            Section {
                TextField("如：4K 影剧、MT 专搜", text: $draft.name)
                    .onChange(of: draft.name) { _, v in if v.count > 16 { draft.name = String(v.prefix(16)) } }
                    .accessibilityIdentifier("preset-name")
            } header: {
                Text("名称（1~16 字）")
            }

            Section {
                SettingsBFlow {
                    ForEach(SettingsBSitePresetCategory.options, id: \.value) { opt in
                        SettingsBSelectChip(title: opt.label, selected: draft.categories.contains(opt.value),
                                            identifier: "preset-category-\(opt.value)") {
                            toggle(&draft.categories, opt.value)
                        }
                    }
                }
                .padding(.vertical, 4)
            } header: {
                Text("资源分类")
            } footer: {
                Text("不勾选 = 不限分类")
            }

            Section {
                if let siteOptions {
                    if siteOptions.isEmpty {
                        Text("还没有接入任何站点；先切到「站点接入」标签页添加，或直接保存（默认搜全部可用站点）。")
                            .font(.subheadline).foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        SettingsBFlow {
                            ForEach(siteOptions, id: \.siteId) { site in
                                SettingsBSelectChip(title: site.usable ? site.name : "\(site.name) ·不可用",
                                                    selected: draft.siteIds.contains(site.siteId),
                                                    identifier: "preset-site-\(site.siteId)") {
                                    toggle(&draft.siteIds, site.siteId)
                                }
                                .opacity(site.usable ? 1 : 0.55)
                            }
                        }
                        .padding(.vertical, 4)
                    }
                } else {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("正在加载站点列表…").foregroundStyle(Theme.textMuted)
                    }
                }
            } header: {
                Text("搜索站点")
            } footer: {
                Text("不勾选 = 全部可用站点")
            }

            Section {
                SubsToggleRow(title: "图览模式",
                              hint: "用该分类搜索时，带海报的结果默认以图墙展示（仅部分站点返回海报，如 M-Team）；结果页右上角可随时临时切换。",
                              isOn: $draft.posterMode, identifier: "preset-poster-mode")
                SubsToggleRow(title: "无痕搜索",
                              hint: "用该分类搜索时不写入搜索历史，搜索面板的「最近搜索」不会出现相关记录，适合隐私敏感的分类。",
                              isOn: $draft.skipHistory, identifier: "preset-skip-history")
            }
        }
        .task { await loadSites() }
    }

    private func toggle(_ list: inout [String], _ value: String) {
        if let i = list.firstIndex(of: value) { list.remove(at: i) } else { list.append(value) }
    }

    private func loadSites() async {
        do {
            async let configured = api.siteList()
            async let catalog = api.siteCatalog()
            let (sites, items) = try await (configured, catalog)
            let names = Dictionary(items.map { ($0.siteId, $0.displayName) }, uniquingKeysWith: { a, _ in a })
            siteOptions = sites.map { SiteOption(siteId: $0.siteId, name: names[$0.siteId] ?? $0.siteId, usable: $0.usable) }
        } catch is CancellationError {
        } catch {
            siteOptions = []
        }
    }

    /// 新建追加到列表末尾（默认可见），编辑则原位替换（保留显隐）
    private func save() async {
        let name = draft.name.trimmingCharacters(in: .whitespaces)
        let next: [SettingsBSitePresetTab]
        if let editingId = draft.editingId {
            next = store.tabs.map { tab in
                guard tab.isPreset, tab.id == editingId else { return tab }
                var t = tab
                t.name = name
                t.categories = draft.categories
                t.siteIds = draft.siteIds
                t.posterMode = draft.posterMode
                t.skipHistory = draft.skipHistory
                return t
            }
        } else {
            var t = SettingsBSitePresetTab(type: "preset", id: SettingsBSitePresetStore.newPresetId(), visible: true)
            t.name = name
            t.categories = draft.categories
            t.siteIds = draft.siteIds
            t.posterMode = draft.posterMode
            t.skipHistory = draft.skipHistory
            next = store.tabs + [t]
        }
        error = nil
        if let message = await store.apply(api, next) {
            error = message
        } else {
            dismiss()
        }
    }
}
