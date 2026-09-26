import SwiftUI

/// 搜索首页（对应 Web `components/search-command.tsx` 的命令面板）：各标签根页右上角的放大镜
/// 在当前标签里压栈打开（`AppRoute.searchHome`），结果页接着压在同一个栈里。
///
/// 版式按 iOS 26 系统搜索页的做法（音乐 / 邮件的搜索）：
/// - 输入框：系统搜索栏常驻标题下方，首次进页直接聚焦弹键盘（点了放大镜就是要输入），
///   看完结果返回不再抢焦点；列表一滚就收键盘；
/// - 模式「影视 | 资源 | 媒体库」（按权限裁剪）用系统搜索范围栏（`.searchScopes`，液态玻璃分段），
///   同系统习惯只在搜索栏激活时出现，点一下输入框就能切；
/// - 资源分类（内置分类 + 自定义预设）拆成两种用法，不再是一排看不全的胶囊：
///   - **浏览**：没输关键词时是「浏览最新资源」列表，点一行直接看该分类最新资源（Web 的「留空提交」；
///     iOS 键盘在输入框为空时搜索键是灰的，按不出来）。自定义预设与内置分类同列，预设带范围摘要；
///   - **搜索范围**：记住的分类以系统搜索标记（token）显示在输入框里，回车即在该范围搜索，删掉标记 = 全部分类；
///     输入关键词后列表给出「在其他范围搜索」，点一行就换到那个范围搜（并记住）；
/// - 输入了关键词，列表顶上给一行「搜索“…”」（下注当前范围），收起键盘后也能一点就搜；
/// - 最近搜索（`GET /search/history`）：系统列表行（主标题 + 一行说明），同关键词的多条记录归成一组：
///   主行是最近一条（写它的范围与时间），其余范围缩进列在下面，始终展开、没有折叠箭头——
///   箭头（展开）和整行（打开）挤在同一行会误触（真机反馈），每行只做一件事：点哪行就回放哪条记录。
///   输入即过滤；左滑主行删整组（多条时先确认）、左滑缩进行删单条，段头「清空」；有快照直接看快照；
/// - 模式与分类记在本机（同 Web localStorage 的 `movieclaw.search-palette-state`）；
///   各页签进来时按页签预选模式（`initialMode`，见 `AppTopBar`），预选的模式也记下来：
///   不预选的入口（「我的」）停在搜索页上次停留的模式，而不只是上次手动切过的模式。
struct SearchHomeView: View {
    /// 进页时预选的模式；nil = 沿用上次停留的模式
    var initialMode: SearchVertical?

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
    @FocusState private var focused: Bool
    /// 搜索栏是否处于激活态（系统的 isPresented）：区分「用户删掉范围标记」与「点取消时系统顺手清空标记」
    @State private var searchPresented = false
    /// 刚被清空标记前的分类：若同一轮里搜索栏被取消，说明是系统清的，要还原
    @State private var clearedTabKey: String?
    /// 恢复记住的模式与预选只在进页时做一次：看完结果返回（`.task` 重跑）时再来一遍，
    /// 会把用户在本页切过的模式又拨回页签预选的模式
    @State private var didRestoreState = false

    private static let stateKey = "movieclaw.search-palette-state"

    init(initialMode: SearchVertical? = nil) {
        self.initialMode = initialMode
        _ = Self.tokenAppearance
    }

    /// 搜索栏里范围标记的底色。系统默认用浅灰底配白字，在深色搜索栏里几乎看不清（真机反馈）；
    /// 标记文字固定是白色、改不了，只能把底压暗。取冷银主题的石板蓝：白字清楚、又不抢过关键词。
    /// 搜索栏在 UIKit 导航栏里，SwiftUI 的 `.tint` 够不着，只能走 UIKit 外观代理（全局只设一次）；
    /// App 里只有这一处搜索栏用标记，不影响别的搜索框。
    private static let tokenAppearance: Void = {
        UISearchTextField.appearance().tokenBackgroundColor = UIColor(red: 0.34, green: 0.39, blue: 0.48, alpha: 1)
    }()

    private var trimmedKeyword: String { keyword.trimmingCharacters(in: .whitespaces) }

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
        let needle = trimmedKeyword.lowercased()
        return order.compactMap { map[$0] }.filter { needle.isEmpty || $0.key.contains(needle) }
    }

    /// 资源模式且有权限：才出分类相关的内容与搜索标记
    private var torrentActive: Bool { mode == .torrent && access.available.contains(.torrent) }

    var body: some View {
        List {
            if !trimmedKeyword.isEmpty, access.available.contains(mode) {
                submitSection
            }
            history
            if torrentActive {
                if trimmedKeyword.isEmpty {
                    browseSections
                } else {
                    otherScopesSection
                }
            }
        }
        .listStyle(.insetGrouped)
        .listSectionSpacing(20)
        .contentMargins(.top, 8, for: .scrollContent)
        .scrollDismissesKeyboard(.immediately)
        .appBackground()
        .navigationTitle("搜索")
        .navigationBarTitleDisplayMode(.inline)
        .searchable(text: $keyword, tokens: scopeTokens, isPresented: $searchPresented, placement: .navigationBarDrawer(displayMode: .always), prompt: prompt) { token in
            Text(token.label)
        }
        .searchScopes(Binding(get: { mode }, set: { changeMode($0) }), activation: .onSearchPresentation) {
            // 只有一个可用模式时不出范围栏
            if access.available.count > 1 {
                ForEach(access.available, id: \.self) { Text($0.shortLabel).tag($0) }
            }
        }
        .searchFocused($focused)
        .onChange(of: searchPresented) { _, presented in
            if !presented, let key = clearedTabKey {
                clearedTabKey = nil
                changeTab(key)
            }
        }
        .onSubmit(of: .search) { submit() }
        .autocorrectionDisabled()
        .textInputAutocapitalization(.never)
        .task {
            if !didRestoreState {
                didRestoreState = true
                restoreState()
                if let initialMode { mode = initialMode }
                // 点了放大镜就是要输入：首次进页直接弹键盘；看完结果返回时不再抢焦点
                focused = true
            }
            access = await SearchAccess.resolve(api: api, permissions: permissions)
            // 预选的模式没有权限：退回记住的模式，别让下一行的兜底把本机记忆改掉
            if let initialMode, mode == initialMode, !access.available.contains(mode) { restoreState() }
            if !access.available.contains(mode), let first = access.available.first { changeMode(first) }
            // 权限核对完才落盘：预选的模式没权限时上面已退回，不会把无权限的模式记下来
            saveState()
            tabs = await SearchTabs.visible(api: api, isAdmin: permissions.isAdmin)
            tabsLoaded = true
            // 上次选的分类已隐藏或删除：回退「全部」，避免没有选中项却悄悄按全部搜索
            if tabKey != "all", !tabs.contains(where: { $0.key == tabKey }) { changeTab("all") }
        }
        .task { await loadHistory() }
        .onAppear {
            takeDraft()
            Task { await loadHistory() }
        }
        .accessibilityIdentifier("search-home")
    }

    private var prompt: String {
        switch mode {
        case .media: "搜索电影、剧集…"
        // 搜索栏收起时系统不画范围标记：把当前范围写进占位文字，激活后由标记接替
        case .torrent: (!searchPresented ? currentTab.map { "在「\($0.label)」中搜索…" } : nil) ?? "搜索资源或 IMDb ID…"
        case .library: "搜索已入库的影片…"
        }
    }

    private var modeHint: String {
        switch mode {
        case .media: "在豆瓣与 TMDB 中搜索影视条目。"
        case .torrent: "跨全部已配置站点搜索种子。"
        case .library: "在媒体库中搜索已入库的影片。"
        }
    }

    // MARK: 搜索范围（资源分类）

    /// 当前记住的分类 / 预设；nil = 全部分类
    private var currentTab: SearchTab? {
        tabs.first { $0.key == tabKey }
    }

    /// 输入框里的范围标记：资源模式下记住的分类显示成一枚系统 token；用户删掉它 = 回到全部分类。
    /// 标记只是 `tabKey` 的另一种呈现，状态仍只有 `tabKey` 一份（切走模式时标记自然消失，切回来又出现）。
    ///
    /// 系统在点「取消」退出搜索时会连标记一起清空，这不是用户想换范围：清空若发生在搜索栏已收起之后
    /// 直接忽略；发生在收起之前则先记下原分类，紧接着的收起（`searchPresented` 变 false）里还原。
    private var scopeTokens: Binding<[ScopeToken]> {
        Binding(
            get: {
                guard torrentActive, let tab = currentTab else { return [] }
                return [ScopeToken(id: tab.key, label: tab.label)]
            },
            set: { tokens in
                if let key = tokens.last?.id {
                    changeTab(key)
                    return
                }
                guard searchPresented, tabKey != "all" else { return }
                let backup = tabKey
                clearedTabKey = backup
                changeTab("all")
                // 取消时收起紧跟在清空之后（实测约 0.1 秒）；过了这段还没收起，就是用户删的，丢掉备份
                Task { @MainActor in
                    try? await Task.sleep(for: .milliseconds(600))
                    if clearedTabKey == backup { clearedTabKey = nil }
                }
            }
        )
    }

    /// 输入了关键词：顶上一行直接搜索，下注在哪儿搜
    private var submitSection: some View {
        Section {
            scopeRow(symbol: "magnifyingglass", title: "搜索“\(trimmedKeyword)”", subtitle: submitSubtitle, action: submit)
                .accessibilityIdentifier("search-submit")
        }
    }

    private var submitSubtitle: String {
        switch mode {
        case .media: "豆瓣与 TMDB 影视条目"
        case .torrent: currentTab.map { "在「\($0.label)」中搜索" } ?? "在全部分类中搜索"
        case .library: "已入库的影片"
        }
    }

    /// 输入了关键词（资源模式）：换个范围搜，点一行即按该范围搜索并记住
    private var otherScopesSection: some View {
        Section {
            if tabKey != "all" {
                scopeRow(symbol: TorrentCategories.allSymbol, title: "全部分类") { submit(in: "all") }
            }
            ForEach(tabs.filter { $0.key != tabKey }, id: \.key) { tab in
                scopeRow(symbol: tab.symbol, title: tab.label, subtitle: tab.summary) { submit(in: tab.key) }
            }
        } header: {
            sectionTitle("在其他范围搜索")
        }
        .accessibilityIdentifier("search-other-scopes")
    }

    /// 没输关键词（资源模式）：浏览各分类最新资源。内置分类与自定义预设同在一段（都是「浏览最新资源」），
    /// 按服务端的标签顺序排（设置里两者混排可调）；预设多一行摘要，写清它搜哪些分类、哪些站点
    private var browseSections: some View {
        Section {
            scopeRow(symbol: TorrentCategories.allSymbol, title: "全部分类", chevron: true) { browse(.all) }
            ForEach(tabs, id: \.key) { tab in
                scopeRow(symbol: tab.symbol, title: tab.label, subtitle: tab.summary, chevron: true) { browse(tab.scope) }
            }
        } header: {
            sectionTitle("浏览最新资源")
        } footer: {
            browseFooter
        }
        .accessibilityIdentifier("search-browse")
    }

    private var browseFooter: some View {
        Text("\(modeHint)搜索时输入框里的标记就是搜索范围，删掉即搜全部分类。")
    }

    /// 范围 / 动作行：图标 + 标题（+ 一行说明）(+ 进入箭头)，整行可点
    private func scopeRow(symbol: String, title: String, subtitle: String? = nil, chevron: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Image(systemName: symbol)
                    .font(.body)
                    .foregroundStyle(Theme.accent)
                    .frame(width: 26)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.body)
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                    if let subtitle {
                        Text(subtitle)
                            .font(.subheadline)
                            .foregroundStyle(Theme.textMuted)
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 8)
                if chevron {
                    Image(systemName: "chevron.right")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(Theme.textFaint)
                }
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
    }

    /// 大号段头（同系统音乐 / App Store 搜索页的「最近搜索」）
    private func sectionTitle(_ title: String) -> some View {
        Text(title)
            .font(.title3.weight(.semibold))
            .foregroundStyle(Theme.text)
            .textCase(nil)
            .padding(.bottom, 4)
    }

    // MARK: 最近搜索

    @ViewBuilder
    private var history: some View {
        if let items {
            // 资源模式下面还有浏览列表，不需要空态占位；输入过滤没命中时顶上的「搜索“…”」就是出口
            if items.isEmpty, !torrentActive {
                ContentUnavailableView {
                    Label("还没有搜索记录", systemImage: "magnifyingglass")
                } description: {
                    Text(modeHint)
                }
                .listRowBackground(Color.clear)
            } else if !groups.isEmpty {
                Section {
                    ForEach(groups, id: \.key) { group in
                        groupRow(group)
                    }
                } header: {
                    HStack(alignment: .firstTextBaseline) {
                        sectionTitle("最近搜索")
                        Spacer()
                        Button("清空") {
                            self.items = []
                            Task { try? await api.searchHistoryClear() }
                        }
                        .font(.body)
                        .textCase(nil)
                        .accessibilityIdentifier("history-clear")
                    }
                }
            }
        } else {
            ProgressView()
                .frame(maxWidth: .infinity)
                .listRowBackground(Color.clear)
        }
    }

    /// 一组同关键词的记录：主行是最近一条（带时钟图标，左滑删整组）；同词的其余范围缩进跟在下面
    /// （不带图标、标题写范围，左滑删这一条）。每条记录只出现一次，点哪行就回放哪条
    @ViewBuilder
    private func groupRow(_ group: HistoryGroup) -> some View {
        let latest = group.items[0]
        historyButton(item: latest, title: group.keyword, subtitle: "\(scopeLabel(latest)) · \(detailLine(latest))", showsIcon: true)
            .swipeActions {
                deleteAction(label: "删除搜索历史组：\(group.keyword)") { Task { await removeGroup(group) } }
            }
        ForEach(group.items.dropFirst(), id: \.id) { item in
            historyButton(item: item, title: scopeLabel(item), subtitle: detailLine(item), showsIcon: false)
                // 缩进到主行文字那一列，看得出同属一个关键词
                .padding(.leading, 40)
                .swipeActions {
                    deleteAction(label: "删除搜索历史：\(item.keyword)（\(item.vertical == "titles" ? "影视" : item.label ?? "资源全部")）") {
                        removeOne(item.id)
                    }
                }
        }
    }

    /// 历史行：主标题 + 一行灰色说明（范围 · 多久之前 · 快照），整行可点；图标列与范围行对齐
    private func historyButton(item: API.SearchHistoryItem, title: String, subtitle: String, showsIcon: Bool) -> some View {
        Button {
            pick(item)
        } label: {
            HStack(spacing: 14) {
                if showsIcon {
                    Image(systemName: "clock.arrow.circlepath")
                        .font(.body)
                        .foregroundStyle(Theme.textFaint)
                        .frame(width: 26)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.body)
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("history-row")
    }

    /// 记录的范围：「影视」「资源」「资源 · 电影」
    private func scopeLabel(_ item: API.SearchHistoryItem) -> String {
        if item.vertical == "titles" { return "影视" }
        return item.label.map { "资源 · \($0)" } ?? "资源"
    }

    /// 记录的时间与快照：「4 小时前 · 快照」
    private func detailLine(_ item: API.SearchHistoryItem) -> String {
        let time = SubsFormat.relative(item.lastSearchedAt)
        return item.hasSnapshot ? "\(time) · 快照" : time
    }

    /// 左滑删除：只留垃圾桶图标（文字交给读屏），红底。iOS 26 的滑动按钮只标 destructive 角色时
    /// 画成白色玻璃圆钮，看不出是危险操作（真机反馈），显式染成系统红
    private func deleteAction(label: String, action: @escaping () -> Void) -> some View {
        Button(role: .destructive, action: action) {
            Label("删除", systemImage: "trash")
                .labelStyle(.iconOnly)
        }
        .tint(.red)
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
        let kw = trimmedKeyword
        guard access.available.contains(mode) else { return }
        if mode == .torrent {
            // 资源模式允许空关键词 = 浏览该分类最新资源
            browse(currentTab?.scope ?? .all, keyword: kw)
            return
        }
        // 影视 / 媒体库没有「浏览」语义：空词不提交
        guard !kw.isEmpty else { return }
        router.push(.search(.init(q: kw, tab: mode.routeTab)))
    }

    /// 换到另一个范围搜索（「在其他范围搜索」）：记住新范围，再按它搜
    private func submit(in key: String) {
        changeTab(key)
        submit()
    }

    /// 按范围打开资源结果页；关键词为空 = 浏览该范围最新资源
    private func browse(_ scope: SearchScope, keyword: String = "") {
        router.push(.search(.init(q: keyword, tab: nil, scope: scope.encoded)))
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

    /// 从结果页点搜索词胶囊回来：填回关键词、切回当时的垂直与范围，弹出键盘等用户改词
    private func takeDraft() {
        guard let draft = router.searchDraft else { return }
        router.searchDraft = nil
        didRestoreState = true
        keyword = draft.keyword
        changeMode(draft.mode)
        if let scope = draft.scope {
            if scope == .all {
                changeTab("all")
            } else if let tab = tabs.first(where: { $0.scope == scope }) {
                changeTab(tab.key)
            }
        }
        // 去结果页前输入框还是聚焦态，直接再设 true 没有变化、不会弹键盘：先撤掉，等退回动画走完再聚焦
        focused = false
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(350))
            focused = true
        }
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

/// 输入框里的资源范围标记（系统搜索 token），id 即 `SearchTab.key`
private struct ScopeToken: Identifiable, Hashable {
    var id: String
    var label: String
}

/// 同关键词（忽略首尾空格与大小写）的历史记录折叠成一组，组内按最近搜索时间倒序
private struct HistoryGroup {
    var key: String
    var keyword: String
    var items: [API.SearchHistoryItem]
}
