import SwiftUI

/// 订阅详情分析页 `/subscriptions/{id}`（对应 Web `components/subscription-inspector-view.tsx`）。
///
/// 页面结构：
/// 1. 摘要卡：订阅类型与状态 → 海报身份 → 配置事实（收录范围 / 自动续订 / 规则组）→ 收录进度条 →
///    操作区（立即搜索、手动选种、更多）；
/// 2. 单视图主体：搜索轮次摘要 → 按季分组的「一集一条履历」→ 排查记录（全量活动折叠区）。
///
/// 刷新节奏同 Web：
/// - 预测后台刷新中（`forecast_pending`）每 1.5 秒只重取详情，最多 40 次；
/// - 有在途投递（grabbed / downloaded）时每 5 秒拉实时下载进度、每 30 秒静默全量刷新；无在途时零请求。
///
/// 「更多」管理面板里的动作：调整订阅、洗一轮版、开关自动续订、更换规则组（管理员）、暂停/恢复、
/// 取消订阅（成员 = 取消关注；管理员 = 带移除预览与删种删文件选项的彻底删除）。
/// `openUpgradeRun` 为 true 时（媒体库「洗版」入口并轨到既有订阅）进入即打开洗一轮版。
struct SubscriptionDetailView: View {
    let subscriptionId: Int
    var openUpgradeRun: Bool = false

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router
    @Environment(Feedback.self) private var feedback
    @Environment(AppModel.self) private var model

    @State private var state: Loadable<API.SubscriptionDetailView> = .loading
    @State private var activities: [API.ActivityView] = []
    @State private var ruleSets: [API.RuleSetView] = []
    @State private var downloads: [String: API.SubscriptionDownloadView] = [:]
    @State private var busy = false
    @State private var sheet: DetailSheet?
    /// 「更多」面板关闭后要执行的动作（面板收起动画结束再弹下一个，避免两个弹层打架）
    @State private var pendingAction: SubscriptionManageSheet.Action?
    @State private var openSeasons: Set<Int> = []
    @State private var seasonsInitialized = false
    @State private var upgradeRunConsumed = false
    @State private var forecastPolls = 0

    enum DetailSheet: Identifiable {
        case manage, adjust, upgradeRun, switchRule, cancel
        case annotate(Int)

        var id: String {
            switch self {
            case .manage: "manage"
            case .adjust: "adjust"
            case .upgradeRun: "upgrade-run"
            case .switchRule: "switch-rule"
            case .cancel: "cancel"
            case let .annotate(season): "annotate-\(season)"
            }
        }
    }

    private var detail: API.SubscriptionDetailView? { state.value }
    private var hasInFlight: Bool { detail?.wanted.contains { $0.status == "grabbed" || $0.status == "downloaded" } ?? false }

    var body: some View {
        Group {
            switch state {
            case .loading:
                VStack(spacing: 12) {
                    ProgressView().controlSize(.large)
                    Text("正在加载订阅详情…").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            case let .failed(message):
                ContentUnavailableView {
                    Label("未能加载该订阅，可能已被删除。", systemImage: "bookmark.slash")
                } description: {
                    Text(message)
                } actions: {
                    Button("重试") { Task { await reload() } }.discoverProminentButton()
                    Button("返回订阅列表", systemImage: "chevron.left") { router.pop() }.buttonStyle(.glass)
                }
                .accessibilityIdentifier("error-state")
            case let .loaded(detail):
                content(detail)
            }
        }
        .appBackground()
        .navigationTitle(detail?.media.title ?? "")
        .navigationBarTitleDisplayMode(.inline)
        .task { await reload() }
        // 预测后台刷新：只重取详情，设上限兜底
        .task(id: detail?.forecastPending ?? false) {
            guard detail?.forecastPending == true else {
                forecastPolls = 0
                return
            }
            while forecastPolls < 40, !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1.5))
                if Task.isCancelled { return }
                forecastPolls += 1
                guard let fresh = try? await api.subscriptionsGet(subscriptionId: subscriptionId) else { continue }
                state = .loaded(fresh)
                if !fresh.forecastPending { return }
            }
        }
        .polling(every: 5, immediately: true) { await refreshDownloads() }
        .polling(every: 30) { if hasInFlight { await reload() } }
        .sheet(item: $sheet, onDismiss: runPendingAction) { sheet in
            sheetContent(sheet)
        }
        .accessibilityIdentifier("subscription-detail")
    }

    // MARK: 数据

    private func reload() async {
        await Loadable.load(into: $state) {
            async let detailTask = api.subscriptionsGet(subscriptionId: subscriptionId)
            async let activitiesTask = api.subscriptionsListActivities(subscriptionId: subscriptionId, limit: 100)
            async let rulesTask: [API.RuleSetView] = permissions.canManageSubscriptions ? api.rulesList() : []
            let (fresh, acts, rules) = try await (detailTask, activitiesTask, rulesTask)
            activities = acts
            ruleSets = rules
            return fresh
        }
        if let detail, !seasonsInitialized {
            // 只在首载时算一次：30 秒静默刷新不把用户手动展开的季刷回去
            openSeasons = WantedLogic.defaultOpenSeasons(detail.wanted)
            seasonsInitialized = true
        }
        if openUpgradeRun, !upgradeRunConsumed, detail != nil, permissions.canSubscribe {
            upgradeRunConsumed = true
            sheet = .upgradeRun
        }
    }

    /// 有在途投递时才拉实时进度（纯读快照）；无在途时零请求
    private func refreshDownloads() async {
        guard hasInFlight else { return }
        if let list = try? await api.subscriptionsListActiveDownloads(subscriptionId: subscriptionId) {
            downloads = Dictionary(list.map { ($0.infoHash, $0) }, uniquingKeysWith: { first, _ in first })
        }
    }

    private func refreshIndex() async {
        await SubscriptionIndex.shared.refresh(api: api, owner: model.session?.username)
    }

    // MARK: 版式

    private func content(_ detail: API.SubscriptionDetailView) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                summaryCard(detail)
                SearchRoundBar(activities: activities, wanted: detail.wanted)
                WantedBreakdown(
                    wanted: detail.wanted,
                    isMovie: detail.media.kind == "movie",
                    downloads: downloads,
                    failures: WantedLogic.pendingFailures(activities),
                    canAnnotate: permissions.isAdmin,
                    openSeasons: $openSeasons,
                    onAnnotate: { sheet = .annotate($0) }
                )
                ActivityLogSection(activities: activities)
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.top, 8)
            .padding(.bottom, 40)
        }
        .refreshable { await reload() }
    }

    private func statusLabel(_ detail: API.SubscriptionDetailView) -> String {
        let label = SubscriptionStatusMeta.label(detail.status)
        let upgrading = detail.progress.upgrading
        // 电影只有一个单元，单单元不带计数
        let upgradingText = detail.progress.total > 1 ? "洗版中（\(upgrading)）" : "洗版中"
        if detail.status == "completed", upgrading > 0 { return "已收齐 · \(upgradingText)" }
        if detail.status == "active", upgrading > 0, detail.progress.wanted == 0 { return "\(label) · \(upgradingText)" }
        return label
    }

    private func ruleSetFact(_ detail: API.SubscriptionDetailView) -> String {
        guard let rule = ruleSets.first(where: { $0.id == detail.ruleSetId }) else { return "#\(detail.ruleSetId)" }
        return rule.upgradeTarget.map { "\(rule.name) · 洗到 \($0)" } ?? rule.name
    }

    private func summaryCard(_ detail: API.SubscriptionDetailView) -> some View {
        let isMovie = detail.media.kind == "movie"
        let poster = api.image(detail.media.posterUrl, .posterCard)
        let mediaRoute = AppRoute.mediaDetail(titleRef: "tmdb:\(detail.media.kind):\(detail.media.tmdbId)")
        return VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(isMovie ? "电影订阅" : "剧集订阅")
                    .font(.caption.weight(.semibold)).tracking(3)
                    .foregroundStyle(Theme.accent2)
                Spacer()
                HStack(spacing: 7) {
                    Circle().fill(detail.status == "active" ? SubsColor.infoSoft : SubscriptionStatusMeta.color(detail.status)).frame(width: 6, height: 6)
                    Text(statusLabel(detail)).font(.subheadline).foregroundStyle(.white.opacity(0.65))
                }
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("subscription-status")
            }
            HStack(alignment: .center, spacing: 16) {
                Button { router.push(mediaRoute) } label: {
                    Color.clear.frame(width: 80, height: 120)
                        .overlay { RemoteImage(url: poster) }
                        .clipShape(.rect(cornerRadius: 10))
                        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.15)))
                        .shadow(color: .black.opacity(0.45), radius: 14, y: 8)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("查看《\(detail.media.title)》详情")
                VStack(alignment: .leading, spacing: 6) {
                    Button { router.push(mediaRoute) } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(detail.media.title).font(.title3.weight(.bold)).foregroundStyle(.white).lineLimit(2).multilineTextAlignment(.leading)
                            if let year = detail.media.year {
                                Text(String(year)).font(.body).monospacedDigit().foregroundStyle(.white.opacity(0.45))
                            }
                        }
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("subscription-title")
                    if !detail.media.originalTitle.isEmpty, detail.media.originalTitle != detail.media.title {
                        Text(detail.media.originalTitle).font(.subheadline).foregroundStyle(.white.opacity(0.55)).lineLimit(1)
                    }
                    Text("订阅于 \(SubsFormat.dateTime(detail.createdAt))").font(.subheadline).foregroundStyle(.white.opacity(0.55))
                }
                Spacer(minLength: 0)
            }

            // 配置事实：稳定的 label / value 列
            HStack(alignment: .top, spacing: 12) {
                fact("收录范围", isMovie ? "正片" : detail.selectedSeasons.isEmpty ? "未勾选季" : "第 \(detail.selectedSeasons.map(String.init).joined(separator: "、")) 季")
                if !isMovie { fact("自动续订", detail.followFuture ? "已开启" : "已关闭") }
                if permissions.canManageSubscriptions { fact("规则组", ruleSetFact(detail)) }
            }
            .padding(.vertical, 12)
            .overlay(alignment: .top) { Divider().overlay(Color.white.opacity(0.08)) }
            .overlay(alignment: .bottom) { Divider().overlay(Color.white.opacity(0.08)) }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("subscription-facts")

            ProgressStrip(progress: detail.progress, unit: isMovie ? "部" : "集")

            actions(detail)
        }
        .padding(16)
        .background {
            ZStack {
                Color(red: 0x0D / 255, green: 0x11 / 255, blue: 0x1B / 255)
                RemoteImage(url: poster).scaleEffect(1.1).blur(radius: 40).opacity(0.25).brightness(-0.3)
                LinearGradient(colors: [Color(red: 7 / 255, green: 10 / 255, blue: 17 / 255).opacity(0.97), Color(red: 10 / 255, green: 14 / 255, blue: 23 / 255).opacity(0.84)], startPoint: .leading, endPoint: .trailing)
            }
        }
        .clipShape(.rect(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Color.white.opacity(0.1)))
        .shadow(color: .black.opacity(0.5), radius: 30, y: 18)
    }

    private func fact(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption).foregroundStyle(.white.opacity(0.4))
            Text(value).font(.subheadline.weight(.medium)).foregroundStyle(.white.opacity(0.85)).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("fact-\(label)")
    }

    @ViewBuilder
    private func actions(_ detail: API.SubscriptionDetailView) -> some View {
        let showSearch = permissions.canSubscribe && detail.progress.wanted > 0 && detail.status != "paused"
        let showManual = permissions.canSubscribe && permissions.canSearch && (detail.progress.wanted > 0 || detail.wanted.contains { $0.upgrade != nil })
        let showMore = permissions.canSubscribe || permissions.canManageSubscriptions
        HStack(spacing: 8) {
            if showSearch {
                Button { Task { await searchNow(detail) } } label: {
                    actionLabel("立即搜索", systemImage: "arrow.clockwise")
                }
                .discoverProminentButton()
                .disabled(busy)
                .accessibilityIdentifier("search-now")
            }
            if showManual {
                // 到站点资源搜索里挑一条种子直接投给本订阅（跳过规则组限制）
                Button { router.push(.search(.init(q: detail.media.title, forSubscription: detail.id))) } label: {
                    actionLabel("手动选种", systemImage: "magnifyingglass")
                }
                .buttonStyle(.glass)
                .accessibilityIdentifier("manual-pick")
            }
            if showMore {
                Button { sheet = .manage } label: {
                    actionLabel("更多", systemImage: "ellipsis")
                }
                .buttonStyle(.glass)
                .disabled(busy)
                .accessibilityIdentifier("subscription-more")
            }
        }
        .font(.subheadline.weight(.semibold))
    }

    /// 三键等宽一行：图标缩小、文字不截断（窄屏自动缩字号而不是出省略号）
    private func actionLabel(_ title: String, systemImage: String) -> some View {
        HStack(spacing: 5) {
            Image(systemName: systemImage).font(.footnote.weight(.semibold))
            Text(title).lineLimit(1).minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 5)
        .padding(.horizontal, -6)
    }

    // MARK: 弹层

    @ViewBuilder
    private func sheetContent(_ sheet: DetailSheet) -> some View {
        if let detail {
            switch sheet {
            case .manage:
                SubscriptionManageSheet(
                    paused: detail.status == "paused",
                    completed: detail.status == "completed",
                    canSubscribe: permissions.canSubscribe,
                    canManage: permissions.canManageSubscriptions,
                    followFuture: detail.media.kind == "movie" ? nil : detail.followFuture,
                    busy: busy
                ) { action in
                    pendingAction = action
                }
            case .adjust:
                SubscriptionAdjustSheet(detail: detail) {
                    Task {
                        await reload()
                        await refreshIndex()
                    }
                }
            case .upgradeRun:
                UpgradeRunSheet(detail: detail) {
                    Task {
                        await reload()
                        await refreshIndex()
                    }
                }
            case .switchRule:
                RuleSetSwitchSheet(ruleSets: ruleSets, currentId: detail.ruleSetId) { id in
                    _ = try await api.subscriptionsUpdate(subscriptionId: detail.id, body: .init(ruleSetId: id))
                    await reload()
                }
            case .cancel:
                SubscriptionCancelSheet(subscriptionId: detail.id, title: detail.media.title) { torrents, files in
                    await confirmRemoval(detail, torrents: torrents, files: files)
                }
            case let .annotate(season):
                MediaSourceAnnotationSheet(mediaItemId: detail.media.mediaItemId, seasonNumber: season, isMovie: detail.media.kind == "movie") { message in
                    // 标注已刷新快照：顺手重跑一轮体检完成排期（幂等），再刷新详情
                    if (try? await api.subscriptionsUpgradeRun(subscriptionId: detail.id, body: .init(ruleSetId: nil))) != nil {
                        feedback.success("\(message)，已重新体检并排期洗版")
                    } else {
                        feedback.success(message)
                    }
                    await reload()
                }
            }
        }
    }

    private func runPendingAction() {
        guard let action = pendingAction, let detail else { return }
        pendingAction = nil
        switch action {
        case .adjust: sheet = .adjust
        case .upgradeRun: sheet = .upgradeRun
        case .switchRule: sheet = .switchRule
        case .toggleFollow: Task { await toggleFollowFuture(detail) }
        case .togglePause: Task { await togglePause(detail) }
        case .remove: Task { await remove(detail) }
        }
    }

    // MARK: 动作

    private func togglePause(_ detail: API.SubscriptionDetailView) async {
        let resuming = detail.status == "paused"
        let ok = await feedback.confirm(
            resuming ? "恢复《\(detail.media.title)》的订阅追踪？" : "暂停《\(detail.media.title)》的订阅追踪？",
            message: resuming
                ? "恢复后会继续搜索缺失资源，并按当前规则自动投递符合条件的结果。"
                : "暂停后不会继续搜索或投递资源；已经提交的下载不受影响，可随时恢复。",
            confirmTitle: resuming ? "恢复追踪" : "暂停追踪"
        )
        guard ok else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await api.subscriptionsSetTrackingState(subscriptionId: detail.id, body: .init(state: resuming ? "active" : "paused"))
            feedback.success(resuming ? "已恢复订阅追踪" : "已暂停订阅追踪")
            await reload()
            await refreshIndex()
        } catch {
            feedback.error(error)
        }
    }

    /// 自动续订是可逆的单一状态动作：直接切换，不打开批量调整
    private func toggleFollowFuture(_ detail: API.SubscriptionDetailView) async {
        let enabling = !detail.followFuture
        busy = true
        defer { busy = false }
        do {
            _ = try await api.subscriptionsSetFollowFuture(subscriptionId: detail.id, body: .init(enabled: enabling))
            feedback.success(enabling ? "已开启自动续订" : "已关闭自动续订")
            await reload()
            await refreshIndex()
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "设置自动续订失败，请稍后重试" : error.localizedDescription)
        }
    }

    /// 立即搜索：缺口跳过冷却重新排队；「暂停中/无可搜缺口」由后端给可读错误
    private func searchNow(_ detail: API.SubscriptionDetailView) async {
        let ok = await feedback.confirm(
            "立即搜索《\(detail.media.title)》的缺失资源？",
            message: "将跳过当前搜索冷却，重新搜索已经可以搜索的缺口。命中当前规则组的资源可能会自动提交下载。",
            confirmTitle: "立即搜索"
        )
        guard ok else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await api.subscriptionsSearchMissingResources(subscriptionId: detail.id)
            feedback.success("\(result.resetCount) 个缺口已重新排队，正在搜索")
            await reload()
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "触发搜索失败，请稍后重试" : error.localizedDescription)
        }
    }

    /// 管理员先问「种子和媒体库文件要不要一起删」；成员只是取消自己的关注
    private func remove(_ detail: API.SubscriptionDetailView) async {
        if permissions.isAdmin {
            sheet = .cancel
            return
        }
        let ok = await feedback.confirm(
            "取消订阅《\(detail.media.title)》？",
            message: "将取消你的订阅关注；已经下载或入库的文件不会被删除。",
            confirmTitle: "取消订阅",
            destructive: true
        )
        guard ok else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await api.subscriptionsUnsubscribe(subscriptionId: detail.id)
            await afterRemoved()
        } catch {
            feedback.error(error)
        }
    }

    private func confirmRemoval(_ detail: API.SubscriptionDetailView, torrents: Bool, files: Bool) async {
        busy = true
        defer { busy = false }
        do {
            let result = try await api.subscriptionsDelete(subscriptionId: detail.id, deleteTorrents: torrents, deleteLibraryFiles: files)
            sheet = nil
            feedback.success(result.cleanupJobId != nil ? "已取消订阅，正在后台清理关联内容（可在任务中心查看进度）" : "已取消订阅")
            await afterRemoved()
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "取消订阅失败，请稍后重试" : error.localizedDescription)
        }
    }

    /// 取消订阅后的共同善后：刷新全站订阅状态并离开这条已不存在的详情
    private func afterRemoved() async {
        await refreshIndex()
        router.pop()
    }
}

// MARK: - 收录进度条

/// 三段式收录进度：绿 = 已入库（洗版中的从中用青色分出）、蓝 = 下载/待入库、底轨 = 仍缺失。
/// 数字图例与颜色同时表达状态，不让色觉成为唯一通道。
struct ProgressStrip: View {
    let progress: API.ProgressView
    let unit: String

    var body: some View {
        let denom = CGFloat(max(progress.total, 1))
        let inPipeline = progress.grabbed + progress.downloaded
        let upgrading = progress.upgrading
        let settled = max(progress.imported - upgrading, 0)
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("收录进度").font(.subheadline.weight(.semibold)).foregroundStyle(.white.opacity(0.85))
                Spacer()
                Text("\(progress.imported) / \(progress.total) \(unit)").font(.subheadline).monospacedDigit().foregroundStyle(.white.opacity(0.55))
            }
            GeometryReader { proxy in
                HStack(spacing: 0) {
                    Rectangle().fill(SubsColor.ok).frame(width: proxy.size.width * CGFloat(settled) / denom)
                    Rectangle().fill(SubsColor.upgrade).frame(width: proxy.size.width * CGFloat(upgrading) / denom)
                    Rectangle().fill(SubsColor.infoSoft).frame(width: proxy.size.width * CGFloat(inPipeline) / denom)
                    Spacer(minLength: 0)
                }
            }
            .frame(height: 6)
            .background(Color.white.opacity(0.12))
            .clipShape(.capsule)
            .accessibilityElement()
            .accessibilityLabel("共 \(progress.total) \(unit)，已入库 \(progress.imported)" + (upgrading > 0 ? "（其中洗版中 \(upgrading)）" : "") + "，下载中 \(inPipeline)，缺失 \(progress.wanted)")
            DiscoverFlowLayout(spacing: 14, lineSpacing: 6) {
                legend(SubsColor.ok, "已入库 \(settled)")
                if upgrading > 0 { legend(SubsColor.upgrade, "洗版中 \(upgrading)") }
                legend(SubsColor.info, "下载中 \(inPipeline)")
                legend(Color.white.opacity(0.2), "缺失 \(progress.wanted)")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("progress-strip")
    }

    private func legend(_ color: Color, _ label: String) -> some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text(label).font(.caption).monospacedDigit().foregroundStyle(.white.opacity(0.45))
        }
    }
}
