import SwiftUI

/// 全局订阅弹层（对应 Web `components/subscribe-dialog.tsx`）：一次点击完成订阅，复杂度沉到默认值。
/// 由 `router.present(.subscribe(...))` 唤起——发现海报、影片详情、搜索结果、AI 卡片、媒体库「洗版」共用。
///
/// 流程对应后端 `POST /subscriptions/title-preview` 的三态：
/// - ready：季勾选 + 自动续订 +（管理员）规则组与入库库 + 投递路由预检 + 快捷新建规则组；
/// - ambiguous：豆瓣收敛歧义，候选海报墙确认一次后带新引用重新预检；
/// - not_found：TMDB 未收录，无法订阅。
/// 已订阅的条目进入管理态：取消订阅（成员 = 取消关注；管理员叠一层带预览的彻底删除）。
///
/// 默认值：剧集勾选全部已播正季（豆瓣季条目采信服务端 suggested_seasons）；在播剧开自动续订；
/// 规则组与入库库取后端按适用范围 / 收藏范围路由的结论，路由选中的不是默认项时才说明「为什么选了它」。
///
/// 洗版变体（`request.upgrade`）：季按库存预填、只列带洗版目标的规则组、自动续订默认关；
/// 建好订阅后立刻跑一轮洗版并在弹层内展示体检报告。
///
/// 交互形态按 iOS 26 表单弹层：绝大多数时候只是确认一下默认值，所以弹层高度跟内容走（半高悬浮，
/// 系统给液态玻璃材质，不自设背景以免盖掉），正文是原生分组列表，左上 ✕ 关闭、右上 ✓ 确认；
/// 「新建规则组」这种低频操作收进规则组菜单末尾。只有洗版体检报告内容多，直接全高。
struct SubscribeSheet: View {
    let request: SubscribeRequest

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(\.dismiss) private var dismiss
    @Environment(Router.self) private var router
    @Environment(AppModel.self) private var model

    @State private var prepared: API.PrepareView?
    @State private var error: String?
    @State private var ruleSets: [API.RuleSetView] = []
    @State private var libraries: [API.LibraryView] = []
    @State private var selectedSeasons: Set<Int> = []
    @State private var followFuture = false
    @State private var ruleSetId: Int?
    @State private var libraryId: Int?
    @State private var busy = false
    @State private var dispatchPreview: API.DispatchPreviewView?
    /// 收藏范围路由的预选结论（用户改选其它库即显式指定，说明消失）
    @State private var routed: (libraryId: Int, reason: String?)?
    /// 规则组适用范围的预选结论
    @State private var ruleRouted: (ruleSetId: Int, reason: String)?
    @State private var upgradeReport: API.UpgradeRunView?
    @State private var creatingRuleSet = false
    @State private var cancelling = false
    /// 表单内容的实际高度（含导航栏与底部安全区），弹层据此贴合内容；量到之前先用半高
    @State private var fitHeight: CGFloat?
    @State private var detent: PresentationDetent = .medium

    private var upgradeMode: Bool { request.upgrade }
    private var canManage: Bool { permissions.canManageSubscriptions }

    /// 入口引用自带的类型（`tmdb:movie:1` / `douban:tv:2`）；豆瓣裸 ID 没有类型，等后端收敛
    private var requestKind: String? {
        let parts = request.titleRef.split(separator: ":").map(String.init)
        return parts.count >= 3 && (parts[1] == "movie" || parts[1] == "tv") ? parts[1] : nil
    }

    private var kind: String { prepared?.media?.kind ?? requestKind ?? "movie" }
    private var displayTitle: String { prepared?.media?.title ?? request.title ?? "" }

    private var selectableRules: [API.RuleSetView] {
        upgradeMode ? ruleSets.filter { $0.upgradeTarget != nil } : ruleSets
    }

    private var canSubmit: Bool {
        guard let media = prepared?.media, !busy else { return false }
        if upgradeMode, !selectableRules.contains(where: { $0.id == ruleSetId }) { return false }
        if media.kind == "movie" { return true }
        return !selectedSeasons.isEmpty || followFuture
    }

    private var showsSubmit: Bool { prepared?.status == "ready" && prepared?.existingSubscriptionId == nil }
    private var showsRules: Bool { upgradeMode || (canManage && !ruleSets.isEmpty) }
    private var showsLibrary: Bool { canManage && !libraries.isEmpty }
    private var pickedRule: API.RuleSetView? { selectableRules.first { $0.id == ruleSetId } }
    private var fitDetent: PresentationDetent { fitHeight.map { .height($0) } ?? .medium }

    var body: some View {
        Group {
            if let upgradeReport {
                SubsSheetScaffold(title: "订阅《\(displayTitle)》", closeTitle: "完成") {
                    UpgradeRunReportView(title: displayTitle, isMovie: kind == "movie", report: upgradeReport)
                } footer: {
                    SubsPrimaryButton(title: "完成", identifier: "upgrade-report-done") { dismiss() }
                }
            } else {
                NavigationStack {
                    Form { content }
                        .scrollContentBackground(.hidden)
                        .scrollBounceBehavior(.basedOnSize)
                        // 表单默认的首尾留白偏大，弹层贴合内容后显得空
                        .contentMargins(.top, 4, for: .scrollContent)
                        .contentMargins(.bottom, 8, for: .scrollContent)
                        // 内容长高、弹层跟着长高时守住顶部：不然剧集长表单会停在底部，条目卡被滚出视野
                        .defaultScrollAnchor(.top, for: .sizeChanges)
                        // 量出整张表单要多高（内容 + 导航栏 + 底部安全区），弹层就开多高
                        .onScrollGeometryChange(for: CGFloat.self) { geometry in
                            geometry.contentSize.height + geometry.contentInsets.top + geometry.contentInsets.bottom
                        } action: { _, height in
                            fit(height)
                        }
                        .navigationTitle(upgradeMode ? "订阅并洗版" : "订阅")
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar { toolbar }
                }
            }
        }
        .presentationDetents([fitDetent, .large], selection: $detent)
        .onChange(of: upgradeReport != nil) { _, showing in
            if showing { detent = .large }
        }
        .interactiveDismissDisabled(busy)
        .accessibilityIdentifier("subscribe-sheet")
        .task { await runPrepare(request.titleRef) }
        // 投递路由预览随「入库库」与「预检收敛出的条目」两者变化重拉（同 Web 依赖 [prepared?.media, libraryId]）：
        // 豆瓣歧义选定候选后库 id 可能不变，只盯库 id 会漏掉这次重拉
        .task(id: "\(libraryId ?? -1)-\(prepared?.media?.tmdbId ?? -1)") { await refreshDispatchPreview() }
        .sheet(isPresented: $creatingRuleSet) {
            RuleSetEditorSheet(ruleSet: nil) { saved in
                ruleSets.append(saved)
                // 洗版变体只接受带洗版目标的组；新组没配目标就不抢选中
                if !upgradeMode || saved.upgradeTarget != nil { ruleSetId = saved.id }
            }
        }
        .sheet(isPresented: $cancelling) {
            if let id = prepared?.existingSubscriptionId {
                SubscriptionCancelSheet(subscriptionId: id, title: displayTitle) { torrents, files in
                    await removePermanently(id, torrents: torrents, files: files)
                }
            }
        }
    }

    /// 内容高度变了就跟着改弹层高度；用户已手动拉到全高时不去抢。
    /// 加载中停在半高（免得先缩成一条再涨回去两段动画）；高度封顶在弹层能开的最大值——
    /// 要的比屏幕还高时系统虽会截断，但每次重设都会把列表往底部带（实测剧集长表单停在最底下）
    private func fit(_ height: CGFloat) {
        guard prepared != nil || error != nil else { return }
        let height = min(height.rounded(.up), Self.maxSheetHeight)
        guard height > 0, height != fitHeight else { return }
        let following = detent != .large
        fitHeight = height
        if following { detent = .height(height) }
    }

    /// 弹层能开的最大高度 = 窗口高度 − 顶部安全区（iPhone Air 实测 912 − 68 = 844）
    private static var maxSheetHeight: CGFloat {
        let window = UIApplication.shared.connectedScenes.compactMap { ($0 as? UIWindowScene)?.keyWindow }.first
        guard let window else { return .greatestFiniteMagnitude }
        return window.bounds.height - window.safeAreaInsets.top
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .cancellationAction) {
            Button("取消", systemImage: "xmark", role: .close) { dismiss() }
                .accessibilityIdentifier("sheet-close")
        }
        if showsSubmit {
            ToolbarItem(placement: .confirmationAction) {
                if busy {
                    ProgressView().accessibilityLabel(upgradeMode ? "正在订阅并体检" : "正在订阅")
                } else {
                    Button(upgradeMode ? "订阅并开始洗版" : "确认订阅", systemImage: "checkmark", role: .confirm) {
                        Task { await submit() }
                    }
                    .discoverProminentButton()
                    .disabled(!canSubmit)
                    .accessibilityIdentifier("subscribe-submit")
                }
            }
        }
    }

    // MARK: 正文

    @ViewBuilder
    private var content: some View {
        Section {
            header
        } footer: {
            if upgradeMode {
                Text("洗版通过订阅持续追踪更好的版本：确认后建立订阅并立即体检库里已有的每一集。")
            }
        }
        .listRowBackground(Color.clear)
        .listRowInsets(EdgeInsets(top: 4, leading: 4, bottom: 4, trailing: 4))

        if let error {
            Section {
                Text(error).font(.subheadline).foregroundStyle(SubsTone.error.color)
                    .accessibilityIdentifier("subscribe-error")
            }
        }
        if let prepared {
            switch prepared.status {
            case "not_found":
                Section {
                    Text("TMDB 未收录该条目，暂时无法订阅。订阅依赖 TMDB 的别名与季集数据来匹配站点资源，可尝试在 TMDB 搜索入口确认条目后再订阅。")
                        .font(.subheadline).foregroundStyle(.secondary)
                        .accessibilityIdentifier("subscribe-not-found")
                }
            case "ambiguous":
                Section {
                    candidateWall(prepared.candidates)
                } header: {
                    Text("找到多个可能的条目，请确认你订阅的是哪一部")
                }
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))
            default:
                if let existing = prepared.existingSubscriptionId {
                    manageState(existing)
                } else {
                    form(prepared)
                }
            }
        }
    }

    /// 条目卡：海报 + 片名 + 年份与类型，一眼确认订的是哪一部；加载中在这里转圈
    private var header: some View {
        HStack(spacing: 14) {
            Color.clear
                .frame(width: 56, height: 84)
                .overlay { RemoteImage(url: api.image(prepared?.media?.posterUrl, .posterCard)) }
                .background(Color.white.opacity(0.06))
                .clipShape(.rect(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.white.opacity(0.1)))
            VStack(alignment: .leading, spacing: 4) {
                Text(displayTitle).font(.title3.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(2)
                if let meta = headerMeta {
                    Text(meta).font(.subheadline).foregroundStyle(.secondary)
                }
                if prepared == nil, error == nil {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("正在获取条目信息…")
                    }
                    .font(.footnote).foregroundStyle(.secondary)
                } else if prepared?.movieOwned == true, prepared?.existingSubscriptionId == nil {
                    Label(upgradeMode ? "媒体库已有，将体检现有版本并按需洗版" : "媒体库已有，订阅后不会重复下载", systemImage: "checkmark")
                        .font(.footnote).foregroundStyle(SubsColor.ok)
                }
            }
            Spacer(minLength: 0)
        }
    }

    /// 「2026 · 电影」；类型未收敛（豆瓣裸 ID）时只写年份
    private var headerMeta: String? {
        let kindText = (prepared?.media?.kind ?? requestKind).map { $0 == "movie" ? "电影" : "剧集" }
        let parts = [prepared?.media?.year.map(String.init), kindText].compactMap(\.self)
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func candidateWall(_ candidates: [API.ResolveCandidateView]) -> some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 10, alignment: .top), count: 3), spacing: 14) {
            ForEach(candidates, id: \.tmdbId) { candidate in
                Button {
                    Task { await runPrepare(candidate.titleRef) }
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        Color.clear.aspectRatio(2.0 / 3.0, contentMode: .fit)
                            .overlay { RemoteImage(url: api.image(candidate.posterUrl, .posterCard)) }
                            .clipShape(.rect(cornerRadius: 10))
                            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.1)))
                        Text(candidate.title).font(.subheadline).foregroundStyle(Theme.text.opacity(0.9)).lineLimit(1)
                        Text(candidate.year.map(String.init) ?? "年份未知").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("subscribe-candidate")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("subscribe-ambiguous")
    }

    /// 已订阅：管理态。关闭走左上 ✕；动作是原生列表行，破坏性的「取消订阅」单独一组垫底
    @ViewBuilder
    private func manageState(_ existing: Int) -> some View {
        Section {
            Label("该\(kind == "movie" ? "电影" : "剧集")已在订阅中，movieclaw 正在持续追踪资源。", systemImage: "checkmark.circle.fill")
                .font(.subheadline)
                .foregroundStyle(Theme.text.opacity(0.85))
                .symbolRenderingMode(.multicolor)
                .accessibilityIdentifier("subscribe-existing")
        }
        Section {
            if upgradeMode {
                // 洗版入口进到已有订阅：并入既有订阅，去详情触发一轮
                Button("去洗一轮版", systemImage: "sparkles") {
                    dismiss()
                    router.push(.subscription(id: existing, upgradeRun: true))
                }
                .accessibilityIdentifier("subscribe-go-upgrade")
            }
            Button("查看订阅详情", systemImage: "list.bullet.rectangle") {
                dismiss()
                router.push(.subscription(id: existing))
            }
            .accessibilityIdentifier("subscribe-open-detail")
        }
        Section {
            Button("取消订阅", systemImage: "bell.slash", role: .destructive) {
                Task { await unsubscribe(existing) }
            }
            .disabled(busy)
            .accessibilityIdentifier("subscribe-unsubscribe")
        }
    }

    /// 订阅表单（ready 且未订阅）
    @ViewBuilder
    private func form(_ prepared: API.PrepareView) -> some View {
        if prepared.media?.kind == "tv" {
            Section {
                ForEach(prepared.seasons, id: \.seasonNumber) { season in
                    seasonRow(season)
                }
            } header: {
                Text("选择要收录的季")
            } footer: {
                Text("勾选即要整季（含未播集）")
            }
            Section {
                Toggle("自动续订", isOn: $followFuture)
                    .accessibilityIdentifier("subscribe-follow-future")
            } footer: {
                Text("之后播出的新集、新一季自动加入追踪")
            }
        }

        if showsRules || showsLibrary {
            Section {
                if showsRules { ruleRow }
                if showsLibrary {
                    Picker("入库到", selection: $libraryId) {
                        ForEach(libraries, id: \.id) { library in
                            Text(library.name + (library.isDefault ? "（默认）" : "")).tag(Int?.some(library.id))
                        }
                    }
                    .pickerStyle(.menu)
                    .accessibilityIdentifier("subscribe-library")
                }
            } footer: {
                routingFooter
            }
        }
    }

    /// 原生多选行：右侧对勾表示选中，第二行是播出进度与库存
    private func seasonRow(_ season: API.SeasonOverview) -> some View {
        let checked = selectedSeasons.contains(season.seasonNumber)
        return Button {
            if checked {
                selectedSeasons.remove(season.seasonNumber)
            } else {
                selectedSeasons.insert(season.seasonNumber)
            }
        } label: {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(SubsFormat.seasonName(season.seasonNumber)).foregroundStyle(Theme.text)
                    HStack(spacing: 6) {
                        Text(SeasonPickRow.progress(season)).foregroundStyle(.secondary)
                        if let owned = SeasonPickRow.owned(season) {
                            Text(owned).foregroundStyle(SubsColor.ok.opacity(0.9))
                        }
                    }
                    .font(.footnote).monospacedDigit()
                }
                Spacer(minLength: 4)
                Image(systemName: "checkmark")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(Theme.accentStrong)
                    .opacity(checked ? 1 : 0)
            }
            .contentShape(.rect)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(checked ? .isSelected : [])
        .accessibilityIdentifier("season-\(season.seasonNumber)")
    }

    /// 规则组行：原生菜单行，菜单里单选规则组，末尾是低频的「新建规则组…」
    @ViewBuilder
    private var ruleRow: some View {
        let title = upgradeMode ? "洗版规则" : "资源规则"
        if upgradeMode, selectableRules.isEmpty {
            Text(canManage
                ? "还没有配置洗版目标的规则组——新建一个，在编辑器里选择「洗到哪一档」即可。"
                : "还没有配置洗版目标的规则组，请联系管理员在「设置 → 订阅规则 → 规则组」中配置「洗到哪一档」。")
                .font(.subheadline).foregroundStyle(.secondary)
            if canManage {
                Button("新建规则组…", systemImage: "plus") { creatingRuleSet = true }
                    .accessibilityIdentifier("subscribe-new-ruleset")
            }
        } else {
            Menu {
                Picker(title, selection: $ruleSetId) {
                    ForEach(selectableRules, id: \.id) { rule in
                        Text(rule.name + (rule.isDefault ? "（默认）" : "") + (upgradeMode ? " · 洗到 \(rule.upgradeTarget ?? "")" : ""))
                            .tag(Int?.some(rule.id))
                    }
                }
                if canManage {
                    Divider()
                    Button("新建规则组…", systemImage: "plus") { creatingRuleSet = true }
                        .accessibilityIdentifier("subscribe-new-ruleset")
                }
            } label: {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text(title).foregroundStyle(Theme.text)
                        Spacer(minLength: 8)
                        Text(pickedRule?.name ?? "未选择").lineLimit(1)
                        Image(systemName: "chevron.up.chevron.down").font(.footnote.weight(.medium))
                    }
                    .foregroundStyle(.secondary)
                    // 品质摘要放在行内：行底比脚注背后的毛玻璃实，字才看得清
                    if let picked = pickedRule {
                        let chips = RuleSetText.summary(picked.typedSpec)
                        // 全不限是个危险默认：把风险讲在订阅之前
                        Text(chips.isEmpty
                            ? "该规则组不限任何条件——可能抓到低画质或无人做种的资源，建议在「设置 → 订阅规则 → 规则组」里加上分辨率与做种数限制"
                            : chips.joined(separator: " · "))
                            .font(.footnote)
                            .foregroundStyle(chips.isEmpty ? SubsColor.warn : Theme.textMuted)
                            .multilineTextAlignment(.leading)
                            .accessibilityIdentifier("subscribe-ruleset-summary")
                    }
                }
                .contentShape(.rect)
            }
            .accessibilityIdentifier("subscribe-ruleset")
        }
    }

    /// 规则 / 入库分组的脚注：路由选中非默认项的理由、投递路径。
    /// 脚注背后是毛玻璃，系统次要色太淡，统一提到 textMuted
    @ViewBuilder
    private var routingFooter: some View {
        VStack(alignment: .leading, spacing: 6) {
            // 路由选中的恰好是默认组时理由就是废话，只在选了非默认组时解释；
            // 组名已写在行里，后端理由原文（「按适用范围选用「电影」：电影」）再念一遍组名反而啰嗦
            if showsRules, let ruleRouted, ruleSetId == ruleRouted.ruleSetId, pickedRule?.isDefault == false {
                Label("按适用范围自动选择", systemImage: "sparkles")
            }
            if showsLibrary {
                if let routed, let reason = routed.reason, libraryId == routed.libraryId,
                   libraries.first(where: { $0.id == routed.libraryId })?.isDefault == false {
                    Label(reason, systemImage: "sparkles")
                        .accessibilityIdentifier("subscribe-routed-library")
                }
                if let dispatchPreview { DispatchPreviewNote(preview: dispatchPreview, emphasized: true) }
            }
        }
        .foregroundStyle(Theme.textMuted)
    }

    // MARK: 数据

    /// 预检并按结果初始化表单默认值（候选确认后带新引用再次进入）
    private func runPrepare(_ ref: String) async {
        prepared = nil
        error = nil
        upgradeReport = nil
        do {
            let loadRules = canManage || upgradeMode
            async let resultTask = api.uiSubscriptionsPreviewTitle(body: .init(titleRef: ref))
            async let rulesTask: [API.RuleSetView] = loadRules ? api.rulesList() : []
            async let libsTask: [API.LibraryView] = canManage && requestKind != nil ? api.libraryList(kind: requestKind, scope: "all") : []
            let (result, rules, initialLibs) = try await (resultTask, rulesTask, libsTask)
            // 媒体库与投递路由以后端收敛后的 canonical kind 为准（豆瓣引用可能被收敛成另一类型）
            let resolvedKind = result.media?.kind ?? requestKind ?? "movie"
            let libs = !canManage || resolvedKind == requestKind ? initialLibs : try await api.libraryList(kind: resolvedKind, scope: "all")
            ruleSets = rules
            libraries = libs
            routed = nil
            ruleRouted = nil
            var pickedLibrary = libs.first { $0.isDefault }?.id ?? libs.first?.id
            var pickedRule: Int?
            var pickedReason: String?
            if canManage, result.status == "ready", let media = result.media {
                let preview = try? await api.subscriptionsPreviewDownloadRouting(kind: resolvedKind, libraryId: nil, tmdbId: media.tmdbId)
                if let id = preview?.libraryId, libs.contains(where: { $0.id == id }) {
                    pickedLibrary = id
                    routed = (id, preview?.routeReason)
                }
                if let id = preview?.ruleSetId {
                    pickedRule = id
                    pickedReason = preview?.ruleSetMatched == true ? preview?.ruleSetReason : nil
                }
            }
            let candidates = upgradeMode ? rules.filter { $0.upgradeTarget != nil } : rules
            let scoped = candidates.first { $0.id == pickedRule }
            ruleSetId = (scoped ?? candidates.first { $0.isDefault } ?? candidates.first)?.id
            if let scoped, let pickedReason { ruleRouted = (scoped.id, pickedReason) }
            libraryId = pickedLibrary
            // 默认季：洗版按库存预填；豆瓣季条目采信 suggested_seasons；否则全部已播正季
            let defaultSeasons: [Int] = if upgradeMode {
                result.seasons.filter { $0.ownedCount > 0 }.map(\.seasonNumber)
            } else if !result.suggestedSeasons.isEmpty {
                result.suggestedSeasons
            } else {
                result.seasons.filter { $0.seasonNumber > 0 && $0.airedCount > 0 }.map(\.seasonNumber)
            }
            selectedSeasons = Set(defaultSeasons)
            followFuture = !upgradeMode && resolvedKind == "tv" && result.media?.status == "Returning Series"
            prepared = result
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription.isEmpty ? "预检失败，请稍后重试" : error.localizedDescription
        }
    }

    private func refreshDispatchPreview() async {
        guard canManage, let libraryId, let media = prepared?.media else {
            dispatchPreview = nil
            return
        }
        dispatchPreview = nil
        // 带上条目身份：后端据此渲染条目目录预览（entry_dir），前端不自己拼名字
        dispatchPreview = try? await api.subscriptionsPreviewDownloadRouting(
            kind: media.kind, libraryId: libraryId, tmdbId: media.tmdbId, title: media.title, year: media.year
        )
    }

    private func refreshIndex() async {
        await SubscriptionIndex.shared.refresh(api: api, owner: model.session?.username)
    }

    private func submit() async {
        guard let media = prepared?.media else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            // 提交预检收敛好的 TMDB 引用；豆瓣身份靠 source_title_ref 原样带回
            let created = try await api.subscriptionsCreate(body: .init(
                titleRef: "tmdb:\(media.kind):\(media.tmdbId)",
                sourceTitleRef: request.titleRef.hasPrefix("douban:") ? request.titleRef : nil,
                selectedSeasons: selectedSeasons.sorted(),
                followFuture: followFuture,
                ruleSetId: canManage ? ruleSetId : nil,
                libraryId: canManage ? libraryId : nil
            ))
            await refreshIndex()
            if upgradeMode {
                // 洗版变体：创建成功即接一轮洗版；失败时订阅已建好，报错留在弹层里
                do {
                    upgradeReport = try await api.subscriptionsUpgradeRun(subscriptionId: created.subscription.id, body: .init(ruleSetId: ruleSetId))
                } catch {
                    self.error = "订阅已创建，但触发洗版失败：\(error.localizedDescription.isEmpty ? "请稍后到订阅详情里重试" : error.localizedDescription)"
                }
                return
            }
            dismiss()
        } catch {
            self.error = error.localizedDescription.isEmpty ? "订阅失败，请稍后重试" : error.localizedDescription
        }
    }

    /// 管理员先叠一层问「种子与媒体库资源要不要一起删」；成员只取消自己的关注
    private func unsubscribe(_ id: Int) async {
        if permissions.isAdmin {
            cancelling = true
            return
        }
        busy = true
        defer { busy = false }
        do {
            _ = try await api.subscriptionsUnsubscribe(subscriptionId: id)
            await refreshIndex()
            dismiss()
        } catch {
            self.error = error.localizedDescription.isEmpty ? "取消订阅失败" : error.localizedDescription
        }
    }

    private func removePermanently(_ id: Int, torrents: Bool, files: Bool) async {
        busy = true
        defer { busy = false }
        do {
            _ = try await api.subscriptionsDelete(subscriptionId: id, deleteTorrents: torrents, deleteLibraryFiles: files)
            cancelling = false
            await refreshIndex()
            dismiss()
        } catch {
            cancelling = false
            self.error = error.localizedDescription.isEmpty ? "取消订阅失败" : error.localizedDescription
        }
    }
}
