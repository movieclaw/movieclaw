import SwiftUI

/// 订阅标签根页「我的订阅」：流媒体式版式（2026-09-26 改版，App 独有，列为已接受差异）。
///
/// 页面按时间与意图拆，而不是按「全部 / 剧集 / 电影」拆：
///
///   沉浸 Hero「下一部到手的」（下载中 / 整理中 → 刚刚入库 → 今天 → 最近一次预告，8 秒轮播）
///   → 刚刚入库（16:9 剧照横滑，点一下直接播放，看完即消失）
///   → 日程（今天起一周的日期条 + 当天议程）
///   → 剧集订阅 / 电影订阅（横滑海报，在追的在前，「›」压栈到完整海报墙）
///
/// 页面底色跟着当前那张 Hero 剧照的主色走（`ImmersiveHeroAmbient`），剧照底部渐隐进去，整页像被
/// 这部作品的光照着。链路体检收成右上角的琥珀色警示钮（管理员），不再占首屏一整条横幅。
///
/// 数据：订阅清单直接消费全站订阅索引 `SubscriptionIndex.shared`（订阅弹层里订阅 / 取消后
/// 即时同步）；整周预告（10 秒）、刚刚入库（20 秒）、下载快照（管理员，10 秒）放在与海报墙共用的
/// `SubscriptionsHomeFeed.shared`，排好的整页结果按输入指纹缓存——Hero 轮播、底色切换引起的重绘
/// 不会重排。老版本服务端没有刚刚入库 / 整周预告时，对应版块自动不出现。
/// 判定口径全在 `SubscriptionsHome`（SubscriptionsHomeModel.swift）。
struct SubscriptionsView: View {
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router
    @Environment(AppModel.self) private var model

    @State private var failed = false
    /// 体检整体为 error 时的库错误数；nil = 不亮警示钮
    @State private var healthErrors: Int?
    @State private var heroIndex = 0
    @State private var tint: Color?
    /// 顶部安全区（状态栏 + 顶栏）高度：沉浸 Hero 用等量负边距顶到屏幕物理顶边
    @State private var topInset: CGFloat = 0
    /// 滚过 Hero 之后才恢复顶部滚动边缘效果（Hero 在顶栏下面时由它自带的压暗保证可读）
    @State private var pastHero = false
    /// 连续滚动距离单独放在可观察对象里：只有 Hero 与氛围底读它，页面主体不随每一帧滚动重算
    @State private var scroll = ImmersiveHeroScroll()

    #if DEBUG
    private static var debugSubscribeConsumed = false
    #endif

    private var index: SubscriptionIndex { SubscriptionIndex.shared }
    private var feed: SubscriptionsHomeFeed { SubscriptionsHomeFeed.shared }
    private var all: [API.SubscriptionView]? { index.subscriptions }

    var body: some View {
        let subs = all ?? []
        let state = feed.state(for: subs)
        let slides = state.slides
        let loading = all == nil && !failed
        let immersive = loading || (!failed && !slides.isEmpty)
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if loading {
                    loadingSkeleton
                } else if failed {
                    errorState
                } else if subs.isEmpty {
                    emptyState.padding(.top, 40)
                } else {
                    if !slides.isEmpty {
                        SubsHomeHeroHost(slides: slides, scroll: scroll, index: $heroIndex)
                    }
                    sections(state)
                        .padding(.top, slides.isEmpty ? 12 : 22)
                }
            }
            .padding(.top, immersive ? -topInset : 0)
            .padding(.bottom, 48)
        }
        .scrollIndicators(.hidden)
        .scrollEdgeEffectHidden(immersive && !pastHero, for: .top)
        .onGeometryChange(for: CGFloat.self) { $0.safeAreaInsets.top } action: { topInset = $0 }
        .onScrollGeometryChange(for: CGFloat.self) { $0.contentOffset.y + $0.contentInsets.top } action: { _, offset in
            scroll.offset = offset
        }
        .onScrollGeometryChange(for: Bool.self) { geometry in
            geometry.contentOffset.y + geometry.contentInsets.top > SubsHomeHero.height - topInset - 64
        } action: { _, past in
            pastHero = past
        }
        .background { SubsHomeAmbientHost(tint: immersive ? tint : nil, scroll: scroll) }
        // 标题同其他标签根页：左上角大字（iOS 标签根页规范）；沉浸时叠在 Hero 的顶部压暗上
        .navigationTitle("我的订阅")
        .toolbarTitleDisplayMode(.inlineLarge)
        .toolbar { healthToolbar }
        .refreshable { await reload() }
        .task { await reload() }
        .task(id: tintSource(slides)) { await updateTint(slides) }
        #if DEBUG
        .task {
            // 开发期：-mcSubscribe tmdb:tv:1399 [-mcSubscribeUpgrade YES] 进入订阅页后直接唤起订阅弹层
            // （截图对照与 UI 测试用；与 -mcRoute /subscriptions 搭配）
            guard !Self.debugSubscribeConsumed, let ref = UserDefaults.standard.string(forKey: "mcSubscribe") else { return }
            Self.debugSubscribeConsumed = true
            router.present(.subscribe(SubscribeRequest(titleRef: ref, upgrade: UserDefaults.standard.bool(forKey: "mcSubscribeUpgrade"))))
        }
        #endif
        .polling(every: 10) { if hasSubscriptions { await feed.refreshArrivals(api: api) } }
        .polling(every: 20) { if hasSubscriptions { await feed.refreshRecent(api: api) } }
        .polling(every: 10, immediately: true) { await feed.refreshTasks(api: api, isAdmin: permissions.isAdmin) }
        .tracksSubscriptionIndex()
    }

    // MARK: 版块

    @ViewBuilder
    private func sections(_ state: SubsHomeState) -> some View {
        VStack(alignment: .leading, spacing: 36) {
            if !feed.recent.isEmpty {
                SubsHomeRecentRow(cards: feed.recent)
            }
            if state.days.contains(where: { !$0.entries.isEmpty }) {
                SubsHomeSchedule(days: state.days)
            }
            if !state.tv.isEmpty {
                SubsHomeShelfRow(title: "剧集订阅", kind: "tv", shelf: state.tv)
            }
            if !state.movie.isEmpty {
                SubsHomeShelfRow(title: "电影订阅", kind: "movie", shelf: state.movie)
            }
        }
    }

    private var loadingSkeleton: some View {
        VStack(alignment: .leading, spacing: 28) {
            SubsHomeHeroSkeleton()
            DiscoverRowSkeleton(title: "刚刚入库")
            DiscoverRowSkeleton(title: "剧集订阅")
        }
    }

    private var errorState: some View {
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
    }

    private var emptyState: some View {
        EmptyState(
            systemImage: "bookmark",
            title: "从一部想看的作品开始",
            message: permissions.canSubscribe
                ? "去发现页挑选一部剧集或电影，打开详情并点击「订阅追踪」，有合适资源时会自动下载入库。"
                : "当前账号暂未开启订阅权限，请联系管理员为你开启。",
            actionTitle: permissions.canSubscribe ? "去发现剧集" : nil,
            action: permissions.canSubscribe ? { router.push(.discover(kind: "tv")) } : nil
        )
        .accessibilityIdentifier("subscriptions-empty")
    }

    /// 链路体检：整体 error 时右上角亮琥珀色警示钮，点开先说清楚是什么问题，再给修复入口
    @ToolbarContentBuilder
    private var healthToolbar: some ToolbarContent {
        if permissions.canManageSubscriptions, let healthErrors {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Section(healthMessage(healthErrors)) {
                        Button("查看体检详情与修复入口", systemImage: "stethoscope") {
                            router.push(.settingsSection(.overview))
                        }
                    }
                } label: {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .symbolRenderingMode(.hierarchical)
                        .foregroundStyle(SubsColor.warn)
                }
                .accessibilityLabel("订阅链路异常：\(healthMessage(healthErrors))")
                .accessibilityIdentifier("health-banner")
            }
        }
    }

    private func healthMessage(_ errors: Int) -> String {
        errors > 0
            ? "\(errors) 个媒体库的入库链路有问题，相关订阅暂时无法自动下载入库（已下达的任务会自动重试）"
            : "订阅链路尚未就绪（缺少可用的资源站点或下载器），订阅暂时只能记录意愿"
    }

    // MARK: 氛围色

    /// 当前 Hero 那张的画面地址（与 Hero 显示同一个地址，取色命中图片缓存）
    private func tintSource(_ slides: [SubsHomeHeroSlide]) -> URL? {
        guard !slides.isEmpty else { return nil }
        let slide = slides[min(heroIndex, slides.count - 1)]
        return api.server.originalTMDBImageURL(slide.media.backdropUrl) ?? api.image(slide.media.posterUrl)
    }

    private func updateTint(_ slides: [SubsHomeHeroSlide]) async {
        guard let url = tintSource(slides) else { return }
        if let color = await ImmersiveHeroAmbientColor.color(for: url), !Task.isCancelled {
            tint = color
        }
    }

    // MARK: 数据

    private var hasSubscriptions: Bool { !(all ?? []).isEmpty }

    private func reload() async {
        failed = false
        feed.adopt(owner: SubscriptionsHomeFeed.ownerKey(api: api, username: model.session?.username))
        let ok = await index.refresh(api: api, owner: model.session?.username)
        failed = !ok && index.subscriptions == nil
        guard hasSubscriptions else {
            await refreshHealth()
            return
        }
        async let health: Void = refreshHealth()
        async let data: Void = feed.refreshAll(api: api, isAdmin: permissions.isAdmin)
        _ = await (health, data)
    }

    private func refreshHealth() async {
        guard permissions.canManageSubscriptions else {
            healthErrors = nil
            return
        }
        if let health = try? await api.subscriptionsCheckAutomationReadiness() {
            healthErrors = health.status == "error" ? health.errorCount : nil
        } else {
            healthErrors = nil
        }
    }
}

private struct SubsHomeHeroHost: View {
    let slides: [SubsHomeHeroSlide]
    let scroll: ImmersiveHeroScroll
    @Binding var index: Int

    var body: some View {
        SubsHomeHero(slides: slides, scrollOffset: scroll.offset, index: $index)
    }
}

private struct SubsHomeAmbientHost: View {
    let tint: Color?
    let scroll: ImmersiveHeroScroll

    var body: some View {
        ImmersiveHeroAmbient(tint: tint, scrollOffset: scroll.offset)
    }
}
