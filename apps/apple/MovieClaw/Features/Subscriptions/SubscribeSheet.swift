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
/// 规则组与入库库取后端按适用范围 / 收藏范围路由的结论，并以徽标说明「为什么选了它」。
///
/// 洗版变体（`request.upgrade`）：季按库存预填、只列带洗版目标的规则组、自动续订默认关；
/// 建好订阅后立刻跑一轮洗版并在弹层内展示体检报告。
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
    /// 收藏范围路由的预选结论（用户改选其它库即显式指定，徽标消失）
    @State private var routed: (libraryId: Int, reason: String?)?
    /// 规则组适用范围的预选结论
    @State private var ruleRouted: (ruleSetId: Int, reason: String)?
    @State private var upgradeReport: API.UpgradeRunView?
    @State private var creatingRuleSet = false
    @State private var cancelling = false

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

    var body: some View {
        Group {
            if let upgradeReport {
                SubsSheetScaffold(title: "订阅《\(displayTitle)》", closeTitle: "完成") {
                    UpgradeRunReportView(title: displayTitle, isMovie: kind == "movie", report: upgradeReport)
                } footer: {
                    SubsPrimaryButton(title: "完成", identifier: "upgrade-report-done") { dismiss() }
                }
            } else {
                SubsSheetScaffold(
                    title: upgradeMode ? "订阅并洗版" : "订阅追踪",
                    subtitle: headerSubtitle
                ) {
                    content
                } footer: {
                    if prepared?.status == "ready", prepared?.existingSubscriptionId == nil {
                        SubsPrimaryButton(
                            title: busy ? (upgradeMode ? "正在订阅并体检…" : "正在订阅…") : (upgradeMode ? "订阅并开始洗版" : "确认订阅"),
                            busy: busy,
                            enabled: canSubmit,
                            identifier: "subscribe-submit"
                        ) { Task { await submit() } }
                    }
                }
            }
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

    private var headerSubtitle: String {
        let year = (prepared?.media?.year).map { " (\($0))" } ?? ""
        let line = "\(displayTitle)\(year)"
        return upgradeMode ? "\(line)\n洗版通过订阅持续追踪更好的版本：确认后建立订阅并立即体检库里已有的每一集。" : line
    }

    // MARK: 正文

    @ViewBuilder
    private var content: some View {
        if prepared == nil, error == nil {
            HStack(spacing: 10) {
                ProgressView()
                Text("正在获取条目信息…").font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 30)
        }
        if let error {
            SubsNotice(text: error, tone: .error).accessibilityIdentifier("subscribe-error")
        }
        if let prepared {
            switch prepared.status {
            case "not_found":
                Text("TMDB 未收录该条目，暂时无法订阅。订阅依赖 TMDB 的别名与季集数据来匹配站点资源，可尝试在 TMDB 搜索入口确认条目后再订阅。")
                    .font(.subheadline).foregroundStyle(Theme.textMuted)
                    .accessibilityIdentifier("subscribe-not-found")
            case "ambiguous":
                candidateWall(prepared.candidates)
            default:
                if let existing = prepared.existingSubscriptionId {
                    manageState(existing)
                } else {
                    form(prepared)
                }
            }
        }
    }

    private func candidateWall(_ candidates: [API.ResolveCandidateView]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("找到多个可能的条目，请确认你订阅的是哪一部：").font(.subheadline).foregroundStyle(Theme.textMuted)
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
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("subscribe-ambiguous")
    }

    /// 已订阅：管理态
    private func manageState(_ existing: Int) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            Label("该\(kind == "movie" ? "电影" : "剧集")已在订阅中，movieclaw 正在持续追踪资源。", systemImage: "checkmark.circle.fill")
                .font(.subheadline)
                .foregroundStyle(Theme.text.opacity(0.85))
                .symbolRenderingMode(.multicolor)
                .accessibilityIdentifier("subscribe-existing")
            // 同 Web 管理态：一行右对齐，好的 →（洗版入口时）去洗一轮版 → 取消订阅
            HStack(spacing: 10) {
                Spacer(minLength: 0)
                Button("好的") { dismiss() }
                    .buttonStyle(.glass)
                    .accessibilityIdentifier("subscribe-ok")
                if upgradeMode {
                    // 洗版入口进到已有订阅：并入既有订阅，去详情触发一轮
                    Button("去洗一轮版") {
                        dismiss()
                        router.push(.subscription(id: existing, upgradeRun: true))
                    }
                    .discoverProminentButton()
                    .accessibilityIdentifier("subscribe-go-upgrade")
                }
                Button("取消订阅", role: .destructive) {
                    Task { await unsubscribe(existing) }
                }
                .buttonStyle(.glass)
                .tint(SubsColor.danger)
                .disabled(busy)
                .accessibilityIdentifier("subscribe-unsubscribe")
            }
            .font(.subheadline.weight(.medium))
        }
    }

    /// 订阅表单（ready 且未订阅）
    private func form(_ prepared: API.PrepareView) -> some View {
        VStack(alignment: .leading, spacing: 22) {
            if prepared.movieOwned {
                SubsNotice(
                    text: upgradeMode ? "媒体库里已有这部电影，将体检现有版本并按需洗版" : "媒体库里已有这部电影，订阅后不会重复下载",
                    tone: .ok, systemImage: "checkmark"
                )
            }
            if prepared.media?.kind == "tv" {
                VStack(alignment: .leading, spacing: 8) {
                    SubsSectionHeader(title: "选择要收录的季", hint: "勾选即要整季（含未播集）")
                    ForEach(prepared.seasons, id: \.seasonNumber) { season in
                        SeasonPickRow(season: season, checked: selectedSeasons.contains(season.seasonNumber)) {
                            if selectedSeasons.contains(season.seasonNumber) {
                                selectedSeasons.remove(season.seasonNumber)
                            } else {
                                selectedSeasons.insert(season.seasonNumber)
                            }
                        }
                    }
                    SubsToggleRow(title: "自动续订", hint: "之后播出的新集、新一季自动加入追踪", isOn: $followFuture, identifier: "subscribe-follow-future")
                        .padding(.top, 6)
                }
            }

            if upgradeMode || (canManage && !ruleSets.isEmpty) {
                ruleSection
            }

            if canManage, !libraries.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    SubsSectionHeader(title: "入库到")
                    Picker("入库到", selection: $libraryId) {
                        ForEach(libraries, id: \.id) { library in
                            Text(library.name + (library.isDefault ? "（默认）" : "")).tag(Int?.some(library.id))
                        }
                    }
                    .pickerStyle(.menu)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 12))
                    .accessibilityIdentifier("subscribe-library")
                    if let routed, let reason = routed.reason, libraryId == routed.libraryId {
                        Text("自动选库：\(reason)").font(.caption).foregroundStyle(Theme.accent.opacity(0.9))
                            .accessibilityIdentifier("subscribe-routed-library")
                    }
                    if let dispatchPreview { DispatchPreviewNote(preview: dispatchPreview) }
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("subscribe-form")
    }

    private var ruleSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                SubsSectionHeader(title: upgradeMode ? "洗版规则" : "资源规则", hint: upgradeMode ? "只列出配置了洗版目标的组" : nil)
                Spacer()
                if canManage {
                    Button("+ 新建规则组") { creatingRuleSet = true }
                        .font(.subheadline.weight(.medium))
                        .accessibilityIdentifier("subscribe-new-ruleset")
                }
            }
            if upgradeMode, selectableRules.isEmpty {
                SubsNotice(
                    text: canManage
                        ? "还没有配置洗版目标的规则组——点右上角「+ 新建规则组」，在编辑器里选择「洗到哪一档」即可。"
                        : "还没有配置洗版目标的规则组，请联系管理员在「设置 → 订阅规则 → 规则组」中配置「洗到哪一档」。",
                    tone: .neutral
                )
            } else {
                Picker("规则组", selection: $ruleSetId) {
                    ForEach(selectableRules, id: \.id) { rule in
                        Text(rule.name + (rule.isDefault ? "（默认）" : "") + (upgradeMode ? " · 洗到 \(rule.upgradeTarget ?? "")" : ""))
                            .tag(Int?.some(rule.id))
                    }
                }
                .pickerStyle(.menu)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 8).padding(.vertical, 4)
                .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 12))
                .accessibilityIdentifier("subscribe-ruleset")
            }
            if let ruleRouted, ruleSetId == ruleRouted.ruleSetId {
                Text("自动选组：\(ruleRouted.reason)").font(.caption).foregroundStyle(Theme.accent.opacity(0.9))
            }
            if let picked = selectableRules.first(where: { $0.id == ruleSetId }) {
                // 全不限是个危险默认：把风险讲在订阅之前
                SubsSpecChips(
                    chips: RuleSetText.summary(picked.typedSpec),
                    emptyText: "该规则组不限任何条件——可能抓到低画质或无人做种的资源，建议在「设置 → 订阅规则 → 规则组」里加上分辨率与做种数限制",
                    emptyTone: SubsColor.warn.opacity(0.9)
                )
                .accessibilityIdentifier("subscribe-ruleset-summary")
            }
        }
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
