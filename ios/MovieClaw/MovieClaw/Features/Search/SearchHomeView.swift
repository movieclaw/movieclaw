import SwiftUI

/// 搜索标签根页（对应 Web `components/search-command.tsx` 的命令面板）。
///
/// - 输入框：系统搜索栏（`.searchable`，常驻标题下方），回车提交；
/// - 模式分段「影视 | 资源 | 媒体库」（按权限裁剪）；资源模式下多一行分类 / 预设 chips，
///   留空提交 = 浏览该分类的最新资源；
/// - 最近搜索（`GET /search/history`）：同关键词的多条记录折叠成一组（可展开看各范围），输入即过滤，
///   可删单条 / 删整组（多条时先确认）/ 清空；点一条按它自己的垂直回放，有快照直接看快照；
/// - 模式与分类记在本机（同 Web localStorage 的 `movieclaw.search-palette-state`）。
struct SearchHomeView: View {
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router
    @Environment(Feedback.self) private var feedback

    @State private var keyword = ""
    @State private var mode: SearchVertical = .media
    @State private var tabKey = "all"
    @State private var tabs: [SearchTab] = []
    @State private var tabsLoaded = false
    @State private var access = SearchAccess()
    @State private var items: [API.SearchHistoryItem]?
    @State private var collapsed: Set<String> = []
    @FocusState private var focused: Bool

    private static let stateKey = "movieclaw.search-palette-state"

    private var groups: [HistoryGroup] {
        let visible = (items ?? []).filter { $0.vertical == "titles" ? access.canMedia : access.canTorrent }
        var order: [String] = []
        var map: [String: HistoryGroup] = [:]
        for item in visible {
            let key = item.keyword.trimmingCharacters(in: .whitespaces).lowercased()
            if map[key] == nil {
                order.append(key)
                map[key] = HistoryGroup(key: key, keyword: item.keyword, items: [])
            }
            map[key]?.items.append(item)
        }
        let needle = keyword.trimmingCharacters(in: .whitespaces).lowercased()
        return order.compactMap { map[$0] }.filter { needle.isEmpty || $0.key.contains(needle) }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if access.available.count > 1 {
                    Picker("搜索范围", selection: Binding(get: { mode }, set: { changeMode($0) })) {
                        ForEach(access.available, id: \.self) { Text($0.shortLabel).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .accessibilityIdentifier("search-mode")
                }
                if mode == .torrent {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            DiscoverChip(label: "全部", active: tabKey == "all") { changeTab("all") }
                            ForEach(tabs, id: \.key) { tab in
                                DiscoverChip(label: tab.label, active: tabKey == tab.key) { changeTab(tab.key) }
                            }
                        }
                    }
                    .scrollClipDisabled()
                    .accessibilityIdentifier("search-categories")
                }
                Text(modeHint)
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                history
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.top, 8)
            .padding(.bottom, 40)
        }
        .scrollDismissesKeyboard(.interactively)
        .appBackground()
        .navigationTitle("搜索")
        .searchable(text: $keyword, placement: .navigationBarDrawer(displayMode: .always), prompt: prompt)
        .searchFocused($focused)
        .onSubmit(of: .search) { submit() }
        .autocorrectionDisabled()
        .textInputAutocapitalization(.never)
        .task {
            restoreState()
            access = await SearchAccess.resolve(api: api, permissions: permissions)
            if !access.available.contains(mode), let first = access.available.first { changeMode(first) }
            tabs = await SearchTabs.visible(api: api, isAdmin: permissions.isAdmin)
            tabsLoaded = true
            // 上次选的分类已隐藏或删除：回退「全部」，避免没有选中项却悄悄按全部搜索
            if tabKey != "all", !tabs.contains(where: { $0.key == tabKey }) { changeTab("all") }
        }
        .task { await loadHistory() }
        .onAppear { Task { await loadHistory() } }
        .accessibilityIdentifier("search-home")
    }

    private var prompt: String {
        switch mode {
        case .media: "搜索电影、剧集…"
        case .torrent: "搜索资源或 IMDb ID…"
        case .library: "搜索已入库的影片…"
        }
    }

    private var modeHint: String {
        switch mode {
        case .media: "在豆瓣与 TMDB 中搜索影视条目"
        case .torrent: "跨全部已配置站点搜索种子，留空提交 = 浏览该分类最新资源"
        case .library: "在媒体库中搜索已入库的影片"
        }
    }

    // MARK: 最近搜索

    @ViewBuilder
    private var history: some View {
        if let items {
            if !items.isEmpty {
                HStack {
                    Text("最近搜索").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.textMuted)
                    Spacer()
                    Button("清空") {
                        self.items = []
                        Task { try? await api.searchHistoryClear() }
                    }
                    .font(.subheadline)
                    .accessibilityIdentifier("history-clear")
                }
                .padding(.top, 8)
            }
            if items.isEmpty {
                Text("还没有搜索记录，输入关键词回车开始搜索").font(.subheadline).foregroundStyle(Theme.textFaint)
            } else if groups.isEmpty {
                Text("没有匹配「\(keyword.trimmingCharacters(in: .whitespaces))」的搜索记录，回车直接搜索")
                    .font(.subheadline).foregroundStyle(Theme.textFaint)
            }
            VStack(spacing: 2) {
                ForEach(groups, id: \.key) { group in
                    groupRow(group)
                }
            }
        } else {
            ProgressView().frame(maxWidth: .infinity).padding(.top, 20)
        }
    }

    @ViewBuilder
    private func groupRow(_ group: HistoryGroup) -> some View {
        let latest = group.items[0]
        let expanded = group.items.count > 1 && (!keyword.trimmingCharacters(in: .whitespaces).isEmpty || !collapsed.contains(group.key))
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Button {
                    pick(latest)
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "clock.arrow.circlepath").foregroundStyle(Theme.textFaint)
                        Text(group.keyword).foregroundStyle(Theme.text).lineLimit(1)
                        if group.items.count > 1 {
                            let media = group.items.filter { $0.vertical == "titles" }.count
                            let torrents = group.items.count - media
                            DiscoverTag(text: "\(group.items.count) 种范围")
                            Text([media > 0 ? "影视 \(media)" : nil, torrents > 0 ? "资源 \(torrents)" : nil].compactMap { $0 }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                        } else {
                            typeBadges(latest)
                        }
                        if latest.hasSnapshot { DiscoverTag(text: "快照", foreground: Theme.accent2) }
                        Spacer(minLength: 4)
                        Text(SubsFormat.relative(latest.lastSearchedAt)).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                    }
                    .font(.subheadline)
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("history-row")
                if group.items.count > 1 {
                    Button {
                        if collapsed.contains(group.key) { collapsed.remove(group.key) } else { collapsed.insert(group.key) }
                    } label: {
                        Image(systemName: "chevron.right")
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                            .frame(width: 28, height: 28)
                            .expandedHitArea(vertical: 8)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Theme.textMuted)
                    .accessibilityLabel(expanded ? "收起 \(group.keyword) 的搜索范围" : "展开 \(group.keyword) 的搜索范围")
                }
                deleteButton(label: "删除搜索历史组：\(group.keyword)") { Task { await removeGroup(group) } }
            }
            .padding(.vertical, 8)
            if expanded {
                ForEach(group.items, id: \.id) { item in
                    HStack(spacing: 8) {
                        Button {
                            pick(item)
                        } label: {
                            HStack(spacing: 8) {
                                Text(item.vertical == "titles" ? "影视" : "资源 · \(item.label ?? "全部")")
                                    .foregroundStyle(Theme.textMuted)
                                if item.hasSnapshot { DiscoverTag(text: "快照", foreground: Theme.accent2) }
                                Spacer(minLength: 4)
                                Text(SubsFormat.relative(item.lastSearchedAt)).font(.caption).foregroundStyle(Theme.textFaint)
                            }
                            .font(.subheadline)
                            .contentShape(.rect)
                        }
                        .buttonStyle(.plain)
                        deleteButton(label: "删除搜索历史：\(item.keyword)（\(item.vertical == "titles" ? "影视" : item.label ?? "资源全部")）") {
                            removeOne(item.id)
                        }
                    }
                    .padding(.leading, 26)
                    .padding(.vertical, 6)
                }
            }
            Divider().overlay(Theme.line)
        }
    }

    private func typeBadges(_ item: API.SearchHistoryItem) -> some View {
        HStack(spacing: 4) {
            if item.vertical == "titles" {
                DiscoverTag(text: "影视", foreground: Theme.accent2, background: Theme.accentSoft)
            } else {
                DiscoverTag(text: "资源")
                if let label = item.label { DiscoverTag(text: label) }
            }
        }
    }

    private func deleteButton(label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: "xmark")
                .font(.caption.weight(.semibold))
                .frame(width: 32, height: 32)
                .expandedHitArea(vertical: 6)
        }
        .buttonStyle(.plain)
        .foregroundStyle(Theme.textFaint)
        .accessibilityLabel(label)
        .accessibilityIdentifier("history-delete")
    }

    // MARK: 动作

    private func loadHistory() async {
        if let list = try? await api.searchHistoryList(limit: 8) {
            items = list
        } else if items == nil {
            items = []
        }
    }

    private func removeOne(_ id: Int) {
        items?.removeAll { $0.id == id }
        Task { try? await api.searchHistoryDelete(historyId: id) }
    }

    private func removeGroup(_ group: HistoryGroup) async {
        if group.items.count > 1 {
            let ok = await feedback.confirm("删除「\(group.keyword)」的 \(group.items.count) 条搜索记录？", confirmTitle: "删除", destructive: true)
            guard ok else { return }
        }
        let ids = Set(group.items.map(\.id))
        items?.removeAll { ids.contains($0.id) }
        for id in ids { try? await api.searchHistoryDelete(historyId: id) }
    }

    private func submit() {
        let kw = keyword.trimmingCharacters(in: .whitespaces)
        guard access.available.contains(mode) else { return }
        if mode == .torrent {
            // 资源模式允许空关键词 = 浏览该分类最新资源
            let scope = tabs.first { $0.key == tabKey }?.scope ?? .all
            router.push(.search(.init(q: kw, tab: nil, scope: scope.encoded)))
            return
        }
        // 影视 / 媒体库没有「浏览」语义：空词不提交
        guard !kw.isEmpty else { return }
        router.push(.search(.init(q: kw, tab: mode.routeTab)))
    }

    /// 点开一条历史：按记录自身的垂直回放，有快照进快照预览，没有发起实时搜索
    private func pick(_ item: API.SearchHistoryItem) {
        let snapshot = item.hasSnapshot ? item.id : nil
        if item.vertical == "titles" {
            router.push(.search(.init(q: item.keyword, tab: "media", snapshot: snapshot)))
            return
        }
        // 还原发起搜索时的图览模式；能出现在历史里的搜索本来就不是无痕的
        let scope = SearchScope(label: item.label, categories: item.categories, siteIds: item.siteIds, posterMode: item.posterMode)
        router.push(.search(.init(q: item.keyword, tab: nil, scope: scope.encoded, snapshot: snapshot)))
    }

    private func changeMode(_ next: SearchVertical) {
        mode = next
        saveState()
    }

    private func changeTab(_ key: String) {
        tabKey = key
        saveState()
    }

    private func restoreState() {
        guard let data = UserDefaults.standard.data(forKey: Self.stateKey),
              let state = try? JSONDecoder().decode([String: String].self, from: data) else { return }
        if let raw = state["mode"], let value = SearchVertical(rawValue: raw) { mode = value }
        if let key = state["tabKey"], !key.isEmpty { tabKey = key }
    }

    private func saveState() {
        let data = try? JSONEncoder().encode(["mode": mode.rawValue, "tabKey": tabKey])
        UserDefaults.standard.set(data, forKey: Self.stateKey)
    }
}

/// 同关键词（忽略首尾空格与大小写）的历史记录折叠成一组，组内按最近搜索时间倒序
private struct HistoryGroup {
    var key: String
    var keyword: String
    var items: [API.SearchHistoryItem]
}
