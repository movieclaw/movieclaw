import SwiftUI

/// 自定义首页（Web `library-customize-view.tsx`，路由 `/library/customize`）。
///
/// 一张行清单：拖动把手换位、眼睛显隐；可设的行（收藏 / 库行 / 合集行）点名字展开设置——
/// 排序（一个菜单同时定档位与方向：「最近添加」「最早添加」各一条）、名字、「只显示我没看过的」、删除（仅自加行）。
/// 底部「＋ 添加一行 · 从哪来？」选一个库或合集；「恢复默认」存空清单。
///
/// 保存：每次改动先落本地草稿，400ms 防抖后整份 PUT `/ui/preferences`（后端是整体覆盖，
/// 所以要带上主题、侧栏等其余偏好原值）。保存成功写回 `LibraryHomePrefs.shared`，回首页立即生效。
struct LibraryCustomizeView: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var prefs = LibraryHomePrefs.shared

    @State private var libraries: [API.LibraryView]?
    @State private var collections: [API.CollectionView] = []
    @State private var fullPrefs: API.UiPreferencesSetting?
    @State private var loadFailed = false
    @State private var saveError: String?
    @State private var draft: [HomeRows.Row]?
    @State private var expanded: String?
    @State private var saveTask: Task<Void, Never>?

    private var savedRows: [HomeRows.Row] {
        HomeRows.build(prefs: prefs.rows ?? [], libraries: libraries ?? [], collections: collections)
    }

    private var rows: [HomeRows.Row] { draft ?? savedRows }

    var body: some View {
        List {
            Section {
                if libraries != nil {
                    ForEach(rows) { row in
                        RowItem(
                            row: row,
                            expanded: expanded == row.id,
                            onToggle: { withAnimation { expanded = expanded == row.id ? nil : row.id } },
                            onChange: { update(row.id, $0) },
                            onRemove: { remove(row.id) }
                        )
                    }
                    .onMove { from, to in
                        var next = rows
                        next.move(fromOffsets: from, toOffset: to)
                        commit(next)
                    }
                }
            } header: {
                VStack(alignment: .leading, spacing: 6) {
                    Text(libraries == nil ? "正在读取…" : "\(rows.filter { !$0.hidden }.count) 行显示 · \(rows.filter(\.hidden).count) 行隐藏")
                        .accessibilityIdentifier("customize-summary")
                    if loadFailed {
                        Text("读取媒体库与合集失败，行清单可能不完整；返回重进重试。")
                            .foregroundStyle(Theme.warning)
                    }
                    if let saveError {
                        Text(saveError).foregroundStyle(Theme.danger)
                    }
                }
                .textCase(nil)
            }

            if libraries != nil {
                Section {
                    AddRowChips(
                        libraries: (libraries ?? []).filter(\.viewerAccess),
                        // 内置的「我的收藏」等自动合集不进候选（首页已有「我的收藏」这一行）
                        collections: collections.filter { $0.kind == "user" },
                        onHome: Set(rows.compactMap { if case let .collection(c, _, _, _) = $0.kind { c.id } else { nil } }),
                        add: add
                    )
                } header: {
                    Text("＋ 添加一行 · 从哪来？").textCase(nil)
                } footer: {
                    Text("改动即时生效，只影响你自己的首页；效果回首页看。")
                }
            }
        }
        .environment(\.editMode, .constant(.active))
        .scrollContentBackground(.hidden)
        .appBackground()
        .navigationTitle("自定义首页")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("恢复默认") { Task { await restoreDefaults() } }
                    .accessibilityIdentifier("restore-defaults")
            }
        }
        .task { await load() }
        .onDisappear { flushSave() }
    }

    // MARK: 数据

    private func load() async {
        do {
            async let libs = api.libraryList(scope: "all")
            async let cols = api.collectionList()
            async let ui = api.uiPrefsShow()
            let (l, c, u) = try await (libs, cols, ui)
            collections = c
            fullPrefs = u
            prefs.rows = u.home.rows
            libraries = l
            loadFailed = false
        } catch is CancellationError {
        } catch {
            loadFailed = true
            libraries = libraries ?? []
        }
    }

    private func update(_ id: String, _ patch: (inout HomeRows.Row) -> Void) {
        commit(rows.map { row in
            guard row.id == id else { return row }
            var next = row
            patch(&next)
            return next
        })
    }

    private func remove(_ id: String) {
        if expanded == id { expanded = nil }
        commit(rows.filter { $0.id != id })
    }

    private func add(_ row: HomeRows.Row) {
        commit(rows + [row])
        expanded = row.id
    }

    /// 改动落草稿，400ms 防抖后保存
    private func commit(_ next: [HomeRows.Row]) {
        draft = next
        saveError = nil
        saveTask?.cancel()
        saveTask = Task {
            try? await Task.sleep(for: .milliseconds(400))
            if Task.isCancelled { return }
            await save(next)
        }
    }

    /// 离开页面时把还在防抖窗口里的改动立即存掉
    private func flushSave() {
        guard let draft, saveTask != nil else { return }
        saveTask?.cancel()
        saveTask = nil
        let rows = draft
        Task { await save(rows) }
    }

    private func save(_ rows: [HomeRows.Row]) async {
        do {
            let saved = try await savePrefs(HomeRows.toPrefs(rows))
            prefs.rows = saved.home.rows
            fullPrefs = saved
            if draft == rows { draft = nil }
            saveTask = nil
        } catch is CancellationError {
        } catch {
            saveError = error.localizedDescription.isEmpty ? "保存失败，请稍后再试" : error.localizedDescription
        }
    }

    /// 后端整体覆盖：以当前完整偏好为底，只替换 home.rows
    private func savePrefs(_ rows: [API.HomeRowPrefInput]) async throws -> API.UiPreferencesSetting {
        let base: API.UiPreferencesSetting
        if let fullPrefs { base = fullPrefs } else { base = try await api.uiPrefsShow() }
        var input = try JSONDecoder().decode(API.UiPreferencesSettingInput.self, from: JSONEncoder().encode(base))
        input.home = API.HomeUiPrefsInput(rows: rows)
        return try await api.uiPrefsUpdate(body: input)
    }

    private func restoreDefaults() async {
        guard await feedback.confirm("恢复默认布局？", message: "你自己加的行会被移除。", confirmTitle: "恢复默认", destructive: true) else { return }
        saveTask?.cancel()
        saveTask = nil
        expanded = nil
        draft = nil
        do {
            let saved = try await savePrefs([])
            prefs.rows = saved.home.rows
            fullPrefs = saved
        } catch {
            saveError = error.localizedDescription
        }
    }
}

/// 清单里的一行。收起时：名字 · 眼睛；可设的行点名字展开设置（Web `RowItem`）
private struct RowItem: View {
    let row: HomeRows.Row
    let expanded: Bool
    var onToggle: () -> Void
    var onChange: ((inout HomeRows.Row) -> Void) -> Void
    var onRemove: () -> Void

    typealias SortOption = (key: String, reversed: Bool, label: String)

    /// 这一行可选的排序：每个有方向的指标两条（自然方向在前），随机 / 未看优先各一条
    private var sortOptions: [SortOption]? {
        switch row.kind {
        case .favorites:
            return HomeRows.favoritesSorts.flatMap { key -> [SortOption] in
                let preset = HomeRows.favoritesPreset(key)
                return preset.direction == nil ? [(key, false, preset.name(false))] : [(key, false, preset.name(false)), (key, true, preset.name(true))]
            }
        case let .library(library, _, _, _, _, _):
            return options(HomeRows.sorts(for: library.kind))
        case .collection:
            return options(HomeRows.allSorts)
        default:
            return nil
        }
    }

    private func options(_ keys: [String]) -> [SortOption] {
        keys.flatMap { key -> [SortOption] in
            let preset = HomeRows.preset(key)
            return preset.direction == nil ? [(key, false, preset.short(false))] : [(key, false, preset.short(false)), (key, true, preset.short(true))]
        }
    }

    var body: some View {
        let editable = sortOptions != nil
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Button {
                    if editable { onToggle() }
                } label: {
                    HStack(spacing: 8) {
                        Text(row.title)
                            .foregroundStyle(row.hidden ? Theme.textFaint : Theme.text)
                            .lineLimit(1)
                        if row.hidden {
                            Text("已隐藏")
                                .font(.caption2)
                                .foregroundStyle(Theme.textFaint)
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .overlay(Capsule().strokeBorder(Theme.line))
                        }
                        if editable {
                            Image(systemName: "chevron.down")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Theme.textFaint)
                                .rotationEffect(.degrees(expanded ? 180 : 0))
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("row-title-\(row.id)")
                Button {
                    onChange { $0.hidden.toggle() }
                } label: {
                    Image(systemName: row.hidden ? "eye.slash" : "eye")
                        .foregroundStyle(row.hidden ? Theme.textFaint : Theme.textMuted)
                        .frame(width: 36, height: 32)
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(row.hidden ? "显示「\(row.title)」" : "隐藏「\(row.title)」")
                .accessibilityIdentifier("row-visibility-\(row.id)")
            }
            if expanded, let options = sortOptions {
                settings(options)
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func settings(_ options: [SortOption]) -> some View {
        let current = options.first { $0.key == row.sort && $0.reversed == row.reversed }
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 10) {
            GridRow {
                Text("排序").font(.footnote).foregroundStyle(Theme.textMuted)
                Menu {
                    ForEach(options, id: \.label) { option in
                        Button {
                            onChange { r in
                                switch r.kind {
                                case .favorites: r.kind = .favorites(sort: option.key, reversed: option.reversed)
                                case let .library(library, _, _, unwatched, name, builtin):
                                    r.kind = .library(library: library, sort: option.key, reversed: option.reversed,
                                                      unwatched: option.key == "last_played" ? false : unwatched, name: name, builtin: builtin)
                                case let .collection(collection, _, _, name):
                                    r.kind = .collection(collection: collection, sort: option.key, reversed: option.reversed, name: name)
                                default: break
                                }
                            }
                        } label: {
                            if option.label == current?.label { Label(option.label, systemImage: "checkmark") } else { Text(option.label) }
                        }
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text(current?.label ?? "排序")
                        Image(systemName: "chevron.up.chevron.down").font(.caption2)
                    }
                    .font(.subheadline.weight(.medium))
                }
                .accessibilityIdentifier("row-sort")
            }
            switch row.kind {
            case .library, .collection: nameRow(hint: row.defaultTitle)
            default: EmptyView()
            }
        }
        let showUnwatched: Bool = { if case let .library(_, sort, _, _, _, _) = row.kind { sort != "last_played" } else { false } }()
        if showUnwatched || row.removable {
            HStack {
                if case let .library(_, _, _, unwatched, _, _) = row.kind, showUnwatched {
                    Toggle("只显示我没看过的", isOn: Binding(
                        get: { unwatched },
                        set: { value in
                            onChange { r in
                                if case let .library(library, sort, reversed, _, name, builtin) = r.kind {
                                    r.kind = .library(library: library, sort: sort, reversed: reversed, unwatched: value, name: name, builtin: builtin)
                                }
                            }
                        }
                    ))
                    .font(.footnote)
                    .toggleStyle(.switch)
                    .fixedSize()
                    .accessibilityIdentifier("row-unwatched")
                }
                Spacer()
                if row.removable {
                    Button("删除这一行", role: .destructive, action: onRemove)
                        .font(.footnote)
                        .buttonStyle(.borderless)
                        .accessibilityIdentifier("row-remove")
                }
            }
        }
    }

    private func nameRow(hint: String) -> some View {
        GridRow {
            Text("名字").font(.footnote).foregroundStyle(Theme.textMuted)
            TextField(hint, text: Binding(
                get: { row.customName },
                set: { value in
                    let name = String(value.prefix(40))
                    onChange { r in
                        switch r.kind {
                        case let .library(library, sort, reversed, unwatched, _, builtin):
                            r.kind = .library(library: library, sort: sort, reversed: reversed, unwatched: unwatched, name: name, builtin: builtin)
                        case let .collection(collection, sort, reversed, _):
                            r.kind = .collection(collection: collection, sort: sort, reversed: reversed, name: name)
                        default: break
                        }
                    }
                }
            ))
            .textFieldStyle(.roundedBorder)
            .submitLabel(.done)
            .accessibilityIdentifier("row-name")
        }
    }
}

/// 「添加一行」的候选：库 +「最近添加」、合集本身；已在首页的合集置灰
private struct AddRowChips: View {
    let libraries: [API.LibraryView]
    let collections: [API.CollectionView]
    let onHome: Set<Int>
    var add: (HomeRows.Row) -> Void

    var body: some View {
        TrackFlowLayout(spacing: 8, lineSpacing: 8) {
            ForEach(libraries, id: \.id) { library in
                Button("\(library.name)库") { add(HomeRows.newLibraryRow(library)) }
                    .buttonStyle(AddChipStyle())
                    .accessibilityIdentifier("add-row-library-\(library.id)")
            }
            ForEach(collections, id: \.id) { collection in
                let onHome = onHome.contains(collection.id)
                Button {
                    add(HomeRows.newCollectionRow(collection))
                } label: {
                    HStack(spacing: 6) {
                        Text(collection.name)
                        Text(onHome ? "已在首页" : "\(collection.itemCount) 部").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                .buttonStyle(AddChipStyle())
                .disabled(onHome)
                .opacity(onHome ? 0.35 : 1)
            }
        }
        .padding(.vertical, 4)
    }
}

private struct AddChipStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline)
            .foregroundStyle(Theme.textMuted)
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(configuration.isPressed ? Color.white.opacity(0.07) : .clear, in: .capsule)
            .overlay(Capsule().strokeBorder(.white.opacity(0.15)))
    }
}
