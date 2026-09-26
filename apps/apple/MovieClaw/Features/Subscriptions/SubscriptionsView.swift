import SwiftUI

/// 订阅标签根页「我的订阅」（对应 Web `components/subscriptions-view.tsx`）。
///
/// 数据源与 Web 同构：订阅清单直接消费全站订阅索引 `SubscriptionIndex.shared`（唯一数据源——
/// 订阅弹层里订阅/取消后索引刷新，这里的墙面即时同步）；进入本页主动刷新一次。
/// 规则组名（管理员）与媒体库名用各自的列表接口补齐，拼成海报下的「规则组 → 媒体库」流向。
///
/// 版式自上而下：标题与计数 → 链路体检警示横幅（管理员、体检整体 error 时）→
/// 「今日可能入库」时间轴（10 秒轮询）→ 剧集 / 电影分区海报墙（分区标题吸顶）。
/// 全部 / 剧集 / 电影切换放在顶栏中间（Web 手机端同样挂在全局顶栏）。
struct SubscriptionsView: View {
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router
    @Environment(AppModel.self) private var model

    /// all / tv / movie
    @State private var filter = "all"
    @State private var failed = false
    @State private var ruleSets: [API.RuleSetView] = []
    @State private var libraries: [API.LibraryView] = []
    /// 体检整体为 error 时的库错误数；nil = 不亮横幅
    @State private var healthErrors: Int?
    @State private var arrivals: [API.TodayArrivalView]?
    @State private var arrivalsFailed = false
    @State private var tasks: [API.DownloadTaskView] = []
    @State private var now = Date()

    #if DEBUG
    private static var debugSubscribeConsumed = false
    #endif

    private var index: SubscriptionIndex { SubscriptionIndex.shared }
    private var all: [API.SubscriptionView]? { index.subscriptions }
    private var tvSubs: [API.SubscriptionView] { (all ?? []).filter { $0.media.kind == "tv" } }
    private var movieSubs: [API.SubscriptionView] { (all ?? []).filter { $0.media.kind == "movie" } }
    private var visible: [API.SubscriptionView] {
        switch filter {
        case "tv": tvSubs
        case "movie": movieSubs
        default: all ?? []
        }
    }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                header
                if permissions.canManageSubscriptions, let healthErrors {
                    healthBanner(healthErrors)
                }
                if all != nil, !failed, !visible.isEmpty {
                    TodayArrivalsCard(
                        groups: TodayArrivals.groups(arrivals ?? [], filter: filter, tasks: tasks, now: now),
                        loaded: arrivals != nil,
                        failed: arrivalsFailed,
                        filter: filter,
                        trackingCount: visible.filter { $0.status == "active" }.count,
                        hasTvInView: visible.contains { $0.media.kind == "tv" },
                        canSubscribe: permissions.canSubscribe,
                        canOpenTasks: permissions.isAdmin
                    )
                    .padding(.horizontal, Theme.pagePadding)
                    .padding(.top, 16)
                }
                content
            }
            .padding(.bottom, 40)
        }
        .scrollIndicators(.hidden)
        .appBackground()
        // 标题同媒体库：左上角大字页面名（iOS 标签根页规范）；类型切换固定在标题下方
        .navigationTitle("我的订阅")
        .toolbarTitleDisplayMode(.inlineLarge)
        .safeAreaBar(edge: .top, alignment: .leading) {
            Picker("订阅类型", selection: $filter) {
                Text("全部").tag("all")
                Text("剧集").tag("tv")
                Text("电影").tag("movie")
            }
            .pickerStyle(.segmented)
            .frame(width: 180)
            .accessibilityIdentifier("subscription-filter")
            .padding(.horizontal, Theme.pagePadding)
            .padding(.bottom, 6)
        }
        .refreshable { await reload() }
        .task { await reload() }
        #if DEBUG
        .task {
            // 开发期：-mcSubscribe tmdb:tv:1399 [-mcSubscribeUpgrade YES] 进入订阅页后直接唤起订阅弹层
            // （截图对照与 UI 测试用；与 -mcRoute /subscriptions 搭配）
            guard !Self.debugSubscribeConsumed, let ref = UserDefaults.standard.string(forKey: "mcSubscribe") else { return }
            Self.debugSubscribeConsumed = true
            router.present(.subscribe(SubscribeRequest(titleRef: ref, upgrade: UserDefaults.standard.bool(forKey: "mcSubscribeUpgrade"))))
        }
        #endif
        .task { await loadNames() }
        .polling(every: 10) { await refreshArrivals() }
        .polling(every: 10, immediately: true) { await refreshTasks() }
        .tracksSubscriptionIndex()
    }

    // MARK: 数据

    private func reload() async {
        failed = false
        let ok = await index.refresh(api: api, owner: model.session?.username)
        failed = !ok && index.subscriptions == nil
        if permissions.canManageSubscriptions {
            if let health = try? await api.subscriptionsCheckAutomationReadiness() {
                healthErrors = health.status == "error" ? health.errorCount : nil
            } else {
                healthErrors = nil
            }
        } else {
            healthErrors = nil
        }
        await refreshArrivals()
    }

    /// 规则组名（管理员可见边界同详情页）与媒体库名：只为拼「规则组 → 库」流向
    private func loadNames() async {
        libraries = (try? await api.libraryList(scope: "all")) ?? []
        ruleSets = permissions.canManageSubscriptions ? ((try? await api.rulesList()) ?? []) : []
    }

    /// 今日预告：有订阅才取；已有快照时瞬时失败继续保留，不闪成错误态
    private func refreshArrivals() async {
        guard let all, !all.isEmpty else { return }
        now = .now
        do {
            arrivals = try await api.subscriptionsListTodayArrivals()
            arrivalsFailed = false
        } catch is CancellationError {
        } catch {
            if arrivals == nil {
                arrivals = []
                arrivalsFailed = true
            }
        }
    }

    /// 下载任务快照（管理员）：「下载中」的预计时间用下载器实时 ETA 修正
    private func refreshTasks() async {
        guard permissions.isAdmin else { return }
        if let list = try? await api.dlTasks() { tasks = list.items }
    }

    // MARK: 版式

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(countLine + " · movieclaw 会持续追踪并在新资源放出后自动入库")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .accessibilityIdentifier("subscriptions-count")
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 8)
    }

    private var countLine: String {
        filter == "all"
            ? "共 \(visible.count) 部订阅 · \(tvSubs.count) 部剧集 · \(movieSubs.count) 部电影"
            : "共 \(visible.count) 部\(filter == "movie" ? "电影" : "剧集")"
    }

    private func healthBanner(_ errors: Int) -> some View {
        Button {
            router.push(.settingsSection(.overview))
        } label: {
            Text((errors > 0
                ? "\(errors) 个媒体库的入库链路有问题，相关订阅暂时无法自动下载入库（已下达的任务会自动重试）"
                : "订阅链路尚未就绪（缺少可用的资源站点或下载器），订阅暂时只能记录意愿") + "——点击查看体检详情与修复入口 →")
                .font(.subheadline)
                .foregroundStyle(Color(red: 0.99, green: 0.9, blue: 0.54))
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(Color.orange.opacity(0.1), in: .rect(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.orange.opacity(0.25)))
        }
        .buttonStyle(.plain)
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 16)
        .accessibilityIdentifier("health-banner")
    }

    @ViewBuilder
    private var content: some View {
        if all == nil, !failed {
            HStack(spacing: 10) {
                ProgressView()
                Text("正在加载订阅…").font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 80)
        } else if failed {
            // 同 Web：只有一行「订阅列表加载失败」+ 重试（已有旧快照时不进错误态，保留旧内容）
            VStack(spacing: 12) {
                Text("订阅列表加载失败").font(.subheadline).foregroundStyle(Theme.textMuted)
                Button("重试") { Task { await reload() } }
                    .buttonStyle(.glass)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 64)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("error-state")
        } else if visible.isEmpty {
            emptyState.padding(.top, 40)
        } else if filter == "all" {
            if !tvSubs.isEmpty { section(title: "剧集订阅", kind: "tv", subs: tvSubs) }
            if !movieSubs.isEmpty { section(title: "电影订阅", kind: "movie", subs: movieSubs) }
        } else {
            section(title: nil, kind: filter, subs: visible)
        }
    }

    private var emptyState: some View {
        let kindName = filter == "movie" ? "电影" : "剧集"
        return EmptyState(
            systemImage: "bookmark",
            title: (all ?? []).isEmpty ? "从一部想看的作品开始" : "还没有\(kindName)订阅",
            message: permissions.canSubscribe
                ? "去发现页挑选一部\(kindName)，打开详情并点击「订阅追踪」，有合适资源时会自动下载入库。"
                : "当前账号暂未开启订阅权限，请联系管理员为你开启。",
            actionTitle: permissions.canSubscribe ? "去发现\(kindName)" : nil,
            action: permissions.canSubscribe ? { router.push(.discover(kind: filter == "movie" ? "movie" : "tv")) } : nil
        )
        .accessibilityIdentifier("subscriptions-empty")
    }

    private func section(title: String?, kind: String, subs: [API.SubscriptionView]) -> some View {
        Section {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 12, alignment: .top)], alignment: .leading, spacing: 20) {
                ForEach(subs, id: \.id) { sub in
                    SubscriptionPosterCell(
                        sub: sub,
                        ruleSetName: ruleSets.first { $0.id == sub.ruleSetId }?.name,
                        libraryName: libraryName(for: sub)
                    )
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.top, title == nil ? 20 : 12)
        } header: {
            if let title {
                HStack {
                    Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.9))
                    Spacer()
                    Text("\(subs.count) 部")
                        .font(.caption).monospacedDigit()
                        .foregroundStyle(Theme.textMuted)
                        .padding(.horizontal, 10).padding(.vertical, 3)
                        .background(Color.white.opacity(0.04), in: .capsule)
                        .overlay(Capsule().strokeBorder(Color.white.opacity(0.07)))
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .glassEffect(.regular, in: .rect(cornerRadius: 16))
                .padding(.horizontal, Theme.pagePadding)
                .padding(.top, 24)
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("section-\(kind)")
            }
        }
    }

    /// 库名：订阅未指定库时显示该类型默认库
    private func libraryName(for sub: API.SubscriptionView) -> String? {
        if let id = sub.libraryId { return libraries.first { $0.id == id }?.name }
        return libraries.first { $0.isDefault && $0.kind == sub.media.kind }?.name
    }
}

// MARK: - 海报墙单元格

/// 订阅海报：左上状态斜标（已入库 / 已收齐 / 自动续订）、右上「● 洗版 N」、海报底部常驻收录摘要，
/// 海报下方是片名、年份与季范围、「规则组 → 媒体库」流向。点按进订阅详情；长按菜单可去影片详情。
/// 已经全部到手的卡片压暗一档：扫墙时亮着的就是还没到手的那些。
struct SubscriptionPosterCell: View {
    let sub: API.SubscriptionView
    var ruleSetName: String?
    var libraryName: String?

    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    var body: some View {
        let meta = SubscriptionSummary.collectionMeta(sub)
        Button {
            router.push(.subscription(id: sub.id))
        } label: {
            VStack(alignment: .leading, spacing: 0) {
                poster(meta)
                VStack(alignment: .leading, spacing: 2) {
                    Text(sub.media.title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                    Text(metaLine)
                        .font(.caption).monospacedDigit()
                        .foregroundStyle(Theme.textMuted)
                        .lineLimit(1)
                    if let flow {
                        Text(flow)
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                            .lineLimit(1)
                    }
                }
                .padding(.top, 8)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .opacity(SubscriptionSummary.fullyCollected(sub) ? 0.72 : 1)
        .contextMenu {
            Button("查看订阅详情", systemImage: "list.bullet.rectangle") { router.push(.subscription(id: sub.id)) }
            Button("查看影片详情", systemImage: "info.circle") {
                router.push(.mediaDetail(titleRef: "tmdb:\(sub.media.kind):\(sub.media.tmdbId)"))
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText(meta))
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("subscription-cell")
    }

    private var metaLine: String {
        let scope = sub.media.kind == "tv" ? SubscriptionSummary.compactSeasonRange(sub.selectedSeasons) : nil
        return [sub.media.year.map(String.init), scope].compactMap { $0 }.joined(separator: " · ")
    }

    private var flow: String? {
        let parts = [ruleSetName, libraryName].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: "  →  ")
    }

    private func accessibilityText(_ meta: SubscriptionCollectionMeta?) -> String {
        var parts = ["《\(sub.media.title)》"]
        if let ribbon = SubscriptionSummary.ribbon(sub) { parts.append(ribbon.label) }
        if let meta {
            parts.append("\(meta.label)，收录 \(meta.value)")
            if let count = meta.upgradingCount { parts.append("\(count) 集洗版中") }
        }
        if let flow { parts.append(flow) }
        return parts.joined(separator: "，")
    }

    private func poster(_ meta: SubscriptionCollectionMeta?) -> some View {
        Color.clear
            .aspectRatio(2.0 / 3.0, contentMode: .fit)
            .overlay { RemoteImage(url: api.image(sub.media.posterUrl, .posterCard)) }
            .overlay(alignment: .topLeading) {
                if let ribbon = SubscriptionSummary.ribbon(sub) { RibbonBadge(ribbon: ribbon) }
            }
            .overlay(alignment: .topTrailing) {
                if let meta, meta.upgrading, let count = meta.upgradingCount {
                    HStack(spacing: 4) {
                        Circle().fill(SubsColor.upgrade).frame(width: 6, height: 6)
                            .shadow(color: SubsColor.upgrade.opacity(0.7), radius: 3)
                        Text("洗版 \(count)").monospacedDigit()
                    }
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(SubsColor.upgrade)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(.black.opacity(0.4), in: .rect(cornerRadius: 6))
                    .padding(8)
                }
            }
            .overlay(alignment: .bottom) {
                if let meta {
                    HStack(spacing: 8) {
                        Text(meta.label).lineLimit(1).foregroundStyle(.white.opacity(0.8))
                        Spacer(minLength: 4)
                        HStack(spacing: 5) {
                            if meta.tracking {
                                Circle().fill(SubsColor.ok).frame(width: 6, height: 6).shadow(color: SubsColor.ok.opacity(0.55), radius: 3)
                            } else if meta.upgrading {
                                Circle().fill(SubsColor.upgrade).frame(width: 6, height: 6)
                            }
                            Text(meta.value).monospacedDigit().fontWeight(.semibold).foregroundStyle(.white)
                        }
                    }
                    .font(.caption)
                    .padding(.horizontal, 10)
                    .padding(.bottom, 8)
                    .padding(.top, 30)
                    .background(LinearGradient(colors: [.clear, .black.opacity(0.55), .black.opacity(0.9)], startPoint: .top, endPoint: .bottom))
                }
            }
            .clipShape(.rect(cornerRadius: Theme.posterRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.posterRadius).strokeBorder(Color.white.opacity(0.08)))
            .shadow(color: .black.opacity(0.35), radius: 10, y: 6)
    }
}

// MARK: - 今日可能入库

/// 首屏的待入库摘要：回答「我下一次能看到新东西是什么时候」。
/// 今天有安排讲今天；没有就讲后端回退给出的最近一天；一周内都没有则说明订阅仍在追踪。
/// 状态色按「事情有没有真的发生」升级：预计入库中性灰、等待资源琥珀、下载中蓝（发光）、整理中绿。
struct TodayArrivalsCard: View {
    let groups: [TodayArrivalGroup]
    let loaded: Bool
    let failed: Bool
    let filter: String
    let trackingCount: Int
    let hasTvInView: Bool
    let canSubscribe: Bool
    let canOpenTasks: Bool

    @Environment(Router.self) private var router

    private var daysAhead: Int { groups.first?.daysAhead ?? 0 }
    private var isUpcoming: Bool { !groups.isEmpty && daysAhead > 0 }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: isUpcoming ? "calendar" : "clock")
                    .font(.system(size: 16))
                    .foregroundStyle(isUpcoming ? Color.white.opacity(0.55) : Color(red: 0.87, green: 0.84, blue: 1))
                    .frame(width: 34, height: 34)
                    .background(Color.white.opacity(0.055), in: .rect(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.09)))
                VStack(alignment: .leading, spacing: 2) {
                    Text(isUpcoming ? "即将入库" : "今日可能入库").font(.subheadline.weight(.semibold)).foregroundStyle(.white.opacity(0.92))
                    Text(isUpcoming ? "今天没有更新，这是最近的一次" : "播出、出种与下载状态动态估算")
                        .font(.caption).foregroundStyle(.white.opacity(0.38)).lineLimit(1)
                }
                Spacer(minLength: 4)
                if loaded, !failed, !groups.isEmpty {
                    let episodes = groups.reduce(0) { $0 + $1.episodeCount }
                    Text(isUpcoming ? "\(daysAhead == 1 ? "明天" : "\(daysAhead) 天后") · \(groups.count) 部" : "\(groups.count) 部 · \(episodes) 集")
                        .font(.caption).monospacedDigit()
                        .foregroundStyle(.white.opacity(0.48))
                        .padding(.horizontal, 10).padding(.vertical, 4)
                        .background(Color.white.opacity(0.045), in: .capsule)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            Divider().overlay(Color.white.opacity(0.065))
            rows
        }
        .glassEffect(.regular.tint(Color(red: 0.06, green: 0.07, blue: 0.11).opacity(0.5)), in: .rect(cornerRadius: 22))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("today-arrivals")
    }

    @ViewBuilder
    private var rows: some View {
        if !loaded, !failed {
            Text("正在计算预计入库时间…").font(.subheadline).foregroundStyle(Theme.textMuted).padding(16)
        } else if failed {
            Text("入库预告暂时不可用").font(.subheadline).foregroundStyle(Color.orange.opacity(0.7)).padding(16)
        } else if groups.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text(trackingCount == 0 ? "订阅都已收齐或暂停了" : hasTvInView ? "接下来 7 天没有已定档的更新" : "当前没有正在下载或整理的电影")
                    .font(.subheadline).foregroundStyle(.white.opacity(0.68))
                if trackingCount > 0 {
                    Text("\(trackingCount) 部订阅仍在追踪\(hasTvInView ? "，定档或出种后会自动开抓" : "，找到合适资源就会自动开抓")")
                        .font(.caption).foregroundStyle(.white.opacity(0.38))
                } else if canSubscribe {
                    Button("去发现页添加新的追踪目标 →") { router.push(.discover(kind: filter == "movie" ? "movie" : "tv")) }
                        .font(.caption).foregroundStyle(Color(red: 0.87, green: 0.84, blue: 1).opacity(0.85))
                } else {
                    Text("有新的追踪目标时，这里会提前预告").font(.caption).foregroundStyle(.white.opacity(0.38))
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
        } else {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(Array(groups.enumerated()), id: \.element.id) { index, group in
                    row(group, last: index == groups.count - 1)
                }
            }
            .padding(10)
        }
    }

    private func style(_ label: String) -> (node: Color, text: Color, time: Color, glow: Bool) {
        switch label {
        case "等待资源": (SubsColor.warn, SubsColor.warn, SubsColor.warn, false)
        case "下载中": (SubsColor.info, SubsColor.info, SubsColor.info, true)
        case "整理中": (SubsColor.ok, SubsColor.ok, SubsColor.ok, false)
        default: (Color.white.opacity(0.55), Color.white.opacity(0.7), Color.white.opacity(0.82), false)
        }
    }

    private func row(_ group: TodayArrivalGroup, last: Bool) -> some View {
        let s = style(group.presentation.statusLabel)
        return HStack(alignment: .top, spacing: 10) {
            ZStack(alignment: .top) {
                if !last {
                    Rectangle().fill(Color.white.opacity(0.1)).frame(width: 1).padding(.top, 14).frame(maxHeight: .infinity)
                }
                Circle()
                    .fill(s.node)
                    .frame(width: 10, height: 10)
                    .shadow(color: s.glow ? s.node.opacity(0.6) : .clear, radius: 5)
                    .opacity(isUpcoming ? 0.65 : 1)
                    .padding(.top, 5)
            }
            .frame(width: 20)
            VStack(alignment: .leading, spacing: 6) {
                Button {
                    router.push(.subscription(id: group.subscriptionId))
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(group.mediaTitle).font(.subheadline.weight(.medium)).foregroundStyle(.white.opacity(0.86)).lineLimit(1)
                        Text(group.episodeLabel).font(.caption).monospacedDigit().foregroundStyle(.white.opacity(0.38)).lineLimit(1)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                HStack(spacing: 8) {
                    statusPill(group.presentation.statusLabel, color: s.text)
                    Text(group.presentation.timeLabel)
                        .font(.system(size: 15, weight: .semibold)).monospacedDigit()
                        .foregroundStyle(s.time)
                }
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 10)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("today-row")
    }

    @ViewBuilder
    private func statusPill(_ label: String, color: Color) -> some View {
        let pill = HStack(spacing: 2) {
            Text(label)
            if label == "下载中", canOpenTasks { Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold)) }
        }
        .font(.caption.weight(.medium))
        .foregroundStyle(color)
        .padding(.horizontal, 8).padding(.vertical, 2)
        .background(color.opacity(0.1), in: .capsule)
        .overlay(Capsule().strokeBorder(color.opacity(0.22)))
        if label == "下载中", canOpenTasks {
            Button { router.push(.activity(view: "active")) } label: { pill.expandedHitArea(vertical: 13) }.buttonStyle(.plain)
        } else {
            pill
        }
    }
}
