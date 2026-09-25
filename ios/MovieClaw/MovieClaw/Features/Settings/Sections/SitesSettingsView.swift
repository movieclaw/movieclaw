import SwiftUI

/// 设置 → 资源站点（对应 Web `site-config-section.tsx` + `search-settings.tsx` + `extension-settings.tsx`）。
///
/// 页面结构与 Web 一致：顶部一行是健康摘要（Web 放在分区副标题里）+ 两个页签「站点接入 / 搜索分类」+ 右侧主操作
/// （添加站点 / 新建自定义分类）。
/// - 站点接入：下载器拥堵提示 → 已接入站点列表（异常置顶、单开手风琴展开详情、⋯ 菜单收纳全部写操作）→ 浏览器插件卡片；
/// - 搜索分类：内置分类与自定义分类的混排列表，显隐/排序/增删改即时保存。
///
/// 为什么确认框与弹层都挂在根上：行在 `Form` 的懒加载单元格里，行内挂 `.sheet` 会随滚动复用而丢失；
/// 行只发意图（`SettingsBSiteRowActions`），根视图统一执行写请求并回写 `SettingsBSiteStore`。
struct SitesSettingsView: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(Router.self) private var router
    /// 外壳常驻的下载任务快照（拥堵提示零额外请求，同 Web useDownloadTasks）；不在外壳里时为空
    @Environment(ShellBadges.self) private var badges: ShellBadges?

    @State private var store = SettingsBSiteStore()
    @State private var presets = SettingsBSitePresetStore()
    @State private var tab: SettingsBSiteTab = .sites
    /// 当前展开详情的站点（单开手风琴）
    @State private var expanded: String?
    @State private var sheet: SettingsBSiteSheet?
    @State private var presetEditor: SettingsBSitePresetDraft?
    @State private var editMode: EditMode = .inactive

    var body: some View {
        Form {
            Section {
                subtitle
                HStack(spacing: 10) {
                    Picker("视图", selection: $tab) {
                        Text("站点接入").tag(SettingsBSiteTab.sites)
                        Text("搜索分类").tag(SettingsBSiteTab.search)
                    }
                    .pickerStyle(.segmented)
                    .accessibilityIdentifier("sites-tab")
                    primaryAction
                }
            }

            if tab == .sites {
                sitesContent
                SettingsBSiteExtSection(onInstall: { sheet = .extInstall }, onToken: { sheet = .extToken })
            } else {
                SettingsBSitePresetSection(store: presets, editor: $presetEditor, editMode: $editMode)
            }
        }
        .settingsBFormStyle()
        .environment(\.editMode, $editMode)
        .onChange(of: tab) { editMode = .inactive }
        .task { await store.load(api) }
        .task { await presets.load(api) }
        // 有站点处于待验证/验证中时 2.5 秒刷新已接入列表，直到全部落定
        .polling(every: 2.5) {
            if store.hasInProgress { await store.refreshConfigured(api) }
        }
        // 有站点开着刷流时 30 秒刷新刷流统计与索引同步节奏（本地聚合查询，不触达站点）
        .polling(every: 30) {
            if store.anyBoosting { await store.refreshStats(api) }
        }
        .sheet(item: $sheet) { target in
            sheetContent(target).sheetFeedback()
        }
        .sheet(item: $presetEditor) { draft in
            SettingsBSitePresetEditorSheet(draft: draft, store: presets).sheetFeedback()
        }
    }

    // MARK: 头部

    /// 健康摘要（同 Web SitesSectionSubtitle）：有站点时「已接入 X 个站点，全部正常 / Y 个异常需要关注」，否则回落功能介绍
    private var subtitle: some View {
        Group {
            if store.configured.isEmpty {
                Text("站点接入与鉴权、搜索分类、插件 Cookie 同步")
            } else if store.failedCount > 0 {
                Text("已接入 \(store.configured.count) 个站点，\(Text("\(store.failedCount) 个异常需要关注").foregroundStyle(Theme.danger).fontWeight(.medium))")
            } else {
                Text("已接入 \(store.configured.count) 个站点，全部正常")
            }
        }
        .font(.footnote)
        .foregroundStyle(Theme.textMuted)
        .accessibilityIdentifier("sites-subtitle")
    }

    @ViewBuilder
    private var primaryAction: some View {
        if tab == .sites {
            Button {
                sheet = .add
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "plus")
                    Text("添加站点")
                }
                .font(.footnote.weight(.semibold))
                .lineLimit(1)
            }
            .discoverProminentButton()
            .disabled(store.loading)
            .fixedSize()
            .accessibilityIdentifier("site-add")
        } else {
            Button {
                presetEditor = SettingsBSitePresetDraft()
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "plus")
                    Text("新建自定义分类")
                }
                .font(.footnote.weight(.semibold))
                .lineLimit(1)
            }
            .discoverProminentButton()
            .fixedSize()
            .accessibilityIdentifier("preset-create")
        }
    }

    // MARK: 站点接入

    @ViewBuilder
    private var sitesContent: some View {
        if let error = store.loadError {
            Section {
                SettingsBNotice(text: error, tone: .danger)
                SettingsBAsyncButton("重试") { await store.load(api) }
            }
        }

        congestionTip

        Section {
            if store.loading {
                Text("正在加载…").foregroundStyle(Theme.textMuted)
            } else if store.configured.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "server.rack")
                        .font(.title2)
                        .foregroundStyle(Theme.textMuted)
                        .frame(width: 48, height: 48)
                        .background(Color.white.opacity(0.07), in: .rect(cornerRadius: 14))
                    Text("还没有配置任何站点").font(.body.weight(.medium))
                    Text("点击右上角「添加站点」开始接入。").font(.footnote).foregroundStyle(Theme.textMuted)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 24)
                .accessibilityIdentifier("sites-empty")
            } else {
                ForEach(store.ordered, id: \.siteId) { site in
                    SettingsBSiteRow(
                        item: store.item(for: site.siteId),
                        site: site,
                        stats: store.syncStats[site.siteId],
                        boost: store.boostStats[site.siteId],
                        expanded: expanded == site.siteId,
                        busy: store.busySites.contains(site.siteId),
                        onToggle: {
                            withAnimation(.easeOut(duration: 0.2)) {
                                expanded = expanded == site.siteId ? nil : site.siteId
                            }
                        },
                        actions: rowActions
                    )
                }
            }
        }
    }

    /// 下载器拥堵提示（同 Web QueueCongestionTip）：有任务在排队说明活动位满了——新种提交受限、刷流已自动暂停投放，
    /// 引导去调大队列上限。取排队最多的那台下载器作为跳转目标。
    @ViewBuilder
    private var congestionTip: some View {
        let queued = badges?.tasks.downloads.filter { $0.state == "queued" } ?? []
        let counts = Dictionary(grouping: queued.compactMap { t in t.downloaderId.map { ($0, t.downloaderName ?? "#\($0)") } }, by: \.0)
        if let worst = counts.max(by: { $0.value.count < $1.value.count }) {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    Text("下载器「\(worst.value.first?.1 ?? "")」有 \(queued.count) 个任务在排队——活动任务位已满，新种子提交受限，刷流已自动暂停投放。建议调大「最大活动种子数」等队列上限。")
                        .font(.footnote)
                        .foregroundStyle(Theme.warning)
                        .fixedSize(horizontal: false, vertical: true)
                    Button("去调整") { router.push(.settingsSection(.downloaders)) }
                        .font(.footnote.weight(.medium))
                        .buttonStyle(.glass)
                        .tint(Theme.warning)
                        .accessibilityIdentifier("sites-congestion-fix")
                }
                .padding(.vertical, 4)
                .listRowBackground(Theme.warning.opacity(0.1))
            }
        }
    }

    // MARK: 行操作

    private var rowActions: SettingsBSiteRowActions {
        SettingsBSiteRowActions(
            toggleEnabled: { site in
                run(site) { try await api.siteStatusSet(siteId: site.siteId, body: .init(enabled: !site.enabled)) }
            },
            toggleProtected: { site in
                run(site) { try await api.siteProtectionSet(siteId: site.siteId, body: .init(protected: !site.protected)) }
            },
            enableBoost: { site in sheet = .boost(site, .enable) },
            disableBoost: { site in Task { await disableBoost(site) } },
            toggleBoostPaused: { site in
                run(site) { try await api.siteRatioBoostPause(siteId: site.siteId, body: .init(paused: !site.boostPaused)) }
            },
            boostSettings: { site in sheet = .boost(site, .adjust) },
            editAuth: { site in
                expanded = site.siteId
                sheet = .editAuth(site)
            },
            reverify: { site in
                run(site) { try await api.siteVerify(siteId: site.siteId) }
            },
            delete: { site in Task { await delete(site) } }
        )
    }

    /// 行级写操作：后端回传最新站点对象原地替换；失败弹后端中文原因
    private func run(_ site: API.ConfiguredSite, _ op: @escaping () async throws -> API.ConfiguredSite) {
        Task {
            do {
                try await store.mutate(site.siteId, op)
            } catch {
                feedback.error(error)
            }
        }
    }

    /// 关闭刷流：二次确认讲清后果（不删数据，只停新增）
    private func disableBoost(_ site: API.ConfiguredSite) async {
        let name = store.item(for: site.siteId).displayName
        let ok = await feedback.confirm(
            "关闭「\(name)」的自动刷分享率？",
            message: [
                "停止抢该站新发布的免费种子",
                "已在做种的刷流任务全部保留，不删除任何数据",
                "站点索引同步回到正常自适应节奏",
                "重新开启时会再次确认预算与保留期",
            ].map { "• " + $0 }.joined(separator: "\n"),
            confirmTitle: "关闭刷流"
        )
        guard ok else { return }
        run(site) { try await api.siteRatioBoostSet(siteId: site.siteId, body: .init(enabled: false)) }
    }

    private func delete(_ site: API.ConfiguredSite) async {
        let name = store.item(for: site.siteId).displayName
        let ok = await feedback.confirm(
            "删除「\(name)」的配置？",
            message: "该站点将不再参与搜索与订阅投递；在池的刷流任务会转出管理并继续做种。可随时重新接入。",
            confirmTitle: "删除",
            destructive: true
        )
        guard ok else { return }
        do {
            try await store.withBusy(site.siteId) { _ = try await api.siteDelete(siteId: site.siteId) }
            store.remove(site.siteId)
            if expanded == site.siteId { expanded = nil }
        } catch {
            feedback.error(error)
        }
    }

    // MARK: 弹层

    @ViewBuilder
    private func sheetContent(_ target: SettingsBSiteSheet) -> some View {
        switch target {
        case .add:
            SettingsBSiteAddSheet(available: store.available) { store.upsert($0) }
        case let .editAuth(site):
            SettingsBSiteEditAuthSheet(item: store.item(for: site.siteId), site: site) { store.upsert($0) }
        case let .boost(site, mode):
            SettingsBSiteBoostSheet(mode: mode, siteName: store.item(for: site.siteId).displayName, site: site) { store.upsert($0) }
        case .extInstall:
            SettingsBSiteExtInstallSheet {
                // 先收起安装指引再弹令牌窗，避免两个 sheet 抢呈现
                sheet = nil
                Task {
                    try? await Task.sleep(for: .milliseconds(450))
                    sheet = .extToken
                }
            }
        case .extToken:
            SettingsBSiteExtTokenSheet()
        }
    }
}

/// 分区内的两档内容（同 Web TABS）
enum SettingsBSiteTab: Hashable {
    case sites, search
}

/// 根视图统一呈现的弹层
enum SettingsBSiteSheet: Identifiable {
    case add
    case editAuth(API.ConfiguredSite)
    case boost(API.ConfiguredSite, SettingsBSiteBoostSheet.Mode)
    case extInstall
    case extToken

    var id: String {
        switch self {
        case .add: "add"
        case let .editAuth(site): "auth-\(site.siteId)"
        case let .boost(site, mode): "boost-\(site.siteId)-\(mode == .enable ? "enable" : "adjust")"
        case .extInstall: "ext-install"
        case .extToken: "ext-token"
        }
    }
}
