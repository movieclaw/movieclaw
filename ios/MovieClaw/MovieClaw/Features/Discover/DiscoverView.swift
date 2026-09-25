import SwiftUI

/// 发现页（发现标签根页，对应 Web `components/discover-view.tsx`）：Hero 轮播 + 分类横滚行。
///
/// 数据流：先读后端的展示清单 `GET /ui/discovery/{type}?provider=`（声明有哪些分区、每个分区画成
/// hero / ranked-row / poster-row），再把每个分区的 collectionRef 原样交给
/// `GET /discover/collections/{ref}/titles` 并发拉取——先到先渲染，失败的行整行收起；
/// 所有常规行都失败时整页进入错误态（上游不可达给「前往网络设置」）。
///
/// 顶栏：中间「电影 / 剧集」分段（Web 放在底栏附属位；原生不改 MainTabView，放顶栏）、
/// 右侧数据源（TMDB / 豆瓣）与「筛选」（仅 TMDB）。
/// 各视角（类型 × 数据源）的清单与片单结果缓存在 `DiscoverFeedStore` 里，来回切换即时恢复。
struct DiscoverView: View {
    let kind: String

    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    @State private var mediaType: String?
    @State private var source = "tmdb"
    @State private var filters = DiscoveryFilters.empty
    @State private var showsFilter = false
    @State private var store = DiscoverFeedStore()
    /// 顶部安全区（状态栏 + 顶栏）高度：沉浸 Hero 用等量负边距顶到屏幕物理顶边
    @State private var topInset: CGFloat = 0

    private var currentType: String { mediaType ?? (kind == "tv" ? "tv" : "movie") }
    private var feedKey: String { "\(currentType):\(source)" }
    private var filtering: Bool { source == "tmdb" && filters.activeCount > 0 }

    var body: some View {
        let feed = store.feed(mediaType: currentType, provider: source)
        Group {
            if filtering {
                DiscoverFilteredGrid(mediaType: currentType, filters: filters) {
                    filters = .empty
                }
                .id("\(currentType)-\(filters.hashValue)")
            } else if let failure = feed.failure, feed.layout == nil || feed.allRowsFailed {
                DiscoverErrorView(failure: failure) {
                    await feed.reload(api: api)
                }
            } else {
                content(feed)
            }
        }
        .appBackground()
        .toolbar { toolbarContent }
        .navigationBarTitleDisplayMode(.inline)
        .task(id: feedKey) {
            await feed.loadIfNeeded(api: api)
        }
        .tracksSubscriptionIndex()
        .sheet(isPresented: $showsFilter) {
            DiscoverFilterSheet(mediaType: currentType, initial: filters) { filters = $0 }
        }
    }

    @ViewBuilder
    private func content(_ feed: DiscoverFeed) -> some View {
        let immersive = feed.declaresHero && feed.hero?.isEmpty != true
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                if feed.layout == nil {
                    DiscoverHeroSkeleton()
                    DiscoverRowSkeleton(title: " ")
                    DiscoverRowSkeleton(title: " ")
                } else {
                    if feed.declaresHero {
                        if let hero = feed.hero {
                            if !hero.isEmpty { DiscoverHero(items: hero) }
                        } else {
                            DiscoverHeroSkeleton()
                        }
                    }
                    ForEach(feed.rowSections, id: \.collectionRef) { section in
                        row(section, feed: feed)
                    }
                    if currentType == "movie", source == "tmdb" {
                        DiscoverRegionFooter {
                            // 院线地区改了：「正在热映 / 即将上映」随地区而变，清掉全部缓存重拉
                            store.invalidateAll()
                            await feed.reload(api: api)
                        }
                    }
                }
            }
            .padding(.top, immersive || feed.layout == nil ? -topInset : 8)
            .padding(.bottom, 32)
        }
        // 沉浸 Hero 从状态栏与顶栏底下穿过：关掉顶部滚动边缘雾化，由 Hero 自带的顶部压暗保证控件可读
        .scrollEdgeEffectHidden(immersive, for: .top)
        .onGeometryChange(for: CGFloat.self) { $0.safeAreaInsets.top } action: { topInset = $0 }
        .refreshable { await feed.reload(api: api) }
        .accessibilityIdentifier("discover-scroll")
    }

    @ViewBuilder
    private func row(_ section: API.DiscoveryPageSectionView, feed: DiscoverFeed) -> some View {
        switch feed.rows[section.collectionRef] {
        case nil:
            DiscoverRowSkeleton(title: section.title)
        case let .loaded(items) where !items.isEmpty:
            let ref = section.supportsFullListing ? CollectionRef(section.collectionRef) : nil
            DiscoverPosterRow(
                title: section.title,
                items: ref == nil ? items : Array(items.prefix(10)),
                onMore: ref.map { ref in { router.push(ref.route) } }
            )
        default:
            // 失败或空行整行收起
            EmptyView()
        }
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            Picker("内容类型", selection: Binding(get: { currentType }, set: { mediaType = $0 })) {
                Text("电影").tag("movie")
                Text("剧集").tag("tv")
            }
            .pickerStyle(.segmented)
            .frame(width: 124)
            .accessibilityIdentifier("discover-type")
        }
        ToolbarItemGroup(placement: .topBarTrailing) {
            Menu {
                Picker("数据源", selection: $source) {
                    Text("TMDB").tag("tmdb")
                    Text("豆瓣").tag("douban")
                }
            } label: {
                Text(source == "tmdb" ? "TMDB" : "豆瓣")
                    .font(.subheadline.weight(.semibold))
            }
            .accessibilityLabel("数据源：\(source == "tmdb" ? "TMDB" : "豆瓣")")
            .accessibilityIdentifier("discover-source")
            if source == "tmdb" {
                Button {
                    showsFilter = true
                } label: {
                    Image(systemName: "line.3.horizontal.decrease")
                        .overlay(alignment: .topTrailing) {
                            if filters.activeCount > 0 {
                                Text("\(filters.activeCount)")
                                    .font(.system(size: 10, weight: .bold))
                                    .foregroundStyle(.black)
                                    .frame(width: 15, height: 15)
                                    .background(Theme.accent, in: .circle)
                                    .offset(x: 8, y: -8)
                            }
                        }
                }
                .accessibilityLabel(filters.activeCount > 0 ? "筛选，已启用 \(filters.activeCount) 项" : "筛选影片")
                .accessibilityIdentifier("discover-filter")
            }
        }
    }
}

// MARK: - 数据

/// 各视角数据的缓存容器：字典本身不参与观察（在 body 里按需创建不会触发状态写入警告），
/// 每个视角的 `DiscoverFeed` 自己是可观察的。
@Observable
final class DiscoverFeedStore {
    @ObservationIgnored private var feeds: [String: DiscoverFeed] = [:]

    func feed(mediaType: String, provider: String) -> DiscoverFeed {
        let key = "\(mediaType):\(provider)"
        if let feed = feeds[key] { return feed }
        let feed = DiscoverFeed(mediaType: mediaType, provider: provider)
        feeds[key] = feed
        return feed
    }

    /// 院线地区变更：其余视角下次进入时重新拉取
    func invalidateAll() {
        for feed in feeds.values { feed.invalidate() }
    }
}

/// 一个视角（类型 × 数据源）的发现页数据：展示清单 + Hero + 各行片单。
///
/// 三态约定（同 Web）：hero = nil 加载中（出骨架）/ [] 无或失败（收起）/ 有值轮播；
/// rows[ref] 缺省 = 加载中，`.failed` = 失败（收起），`.loaded` = 渲染。
@Observable
final class DiscoverFeed {
    enum RowState { case loaded([DiscoverPosterItem]), failed }

    struct Failure {
        var message: String
        var unreachable: Bool
    }

    let mediaType: String
    let provider: String
    private(set) var layout: API.DiscoveryPageView?
    private(set) var hero: [DiscoverPosterItem]?
    private(set) var rows: [String: RowState] = [:]
    private(set) var failure: Failure?
    @ObservationIgnored private var loaded = false

    init(mediaType: String, provider: String) {
        self.mediaType = mediaType
        self.provider = provider
    }

    var declaresHero: Bool { layout?.sections.contains { $0.presentation == "hero" } == true }
    var rowSections: [API.DiscoveryPageSectionView] { layout?.sections.filter { $0.presentation != "hero" } ?? [] }

    /// 常规行全部失败（且没有任何一行成功）→ 整页错误态
    var allRowsFailed: Bool {
        let sections = rowSections
        guard !sections.isEmpty else { return false }
        return sections.allSatisfy { if case .failed = rows[$0.collectionRef] { true } else { false } }
    }

    /// 首次进入该视角才拉取；已缓存的视角直接恢复
    func loadIfNeeded(api: APIClient) async {
        guard !loaded else { return }
        await reload(api: api)
    }

    func invalidate() {
        loaded = false
    }

    func reload(api: APIClient) async {
        loaded = true
        failure = nil
        let page: API.DiscoveryPageView
        do {
            page = try await api.uiDiscoveryGet(mediaType: mediaType, provider: provider)
        } catch is CancellationError {
            loaded = false
            return
        } catch {
            failure = Failure(message: error.localizedDescription, unreachable: error.isUpstreamUnreachable)
            loaded = false
            return
        }
        layout = page
        let heroRef = page.sections.first { $0.presentation == "hero" }?.collectionRef
        if heroRef == nil { hero = [] }
        var firstError: Error?

        await withTaskGroup(of: (String, Result<[DiscoverPosterItem], Error>).self) { group in
            for section in page.sections {
                let ref = section.collectionRef
                let limit = section.previewLimit
                group.addTask {
                    do {
                        let result = try await api.discoverBrowseCollection(collectionRef: ref, limit: limit)
                        return (ref, .success(result.titles.map(DiscoverPosterItem.init)))
                    } catch {
                        return (ref, .failure(error))
                    }
                }
            }
            for await (ref, result) in group {
                if ref == heroRef {
                    // Hero 失败只收起自身；刷新失败保留旧轮播
                    if let items = try? result.get() { hero = items } else if hero == nil { hero = [] }
                    continue
                }
                switch result {
                case let .success(items):
                    rows[ref] = .loaded(items)
                case let .failure(error):
                    if error is CancellationError { continue }
                    if case .loaded = rows[ref] { continue }
                    rows[ref] = .failed
                    firstError = firstError ?? error
                }
            }
        }
        if allRowsFailed, let firstError {
            failure = Failure(message: firstError.localizedDescription, unreachable: firstError.isUpstreamUnreachable)
            loaded = false
        }
    }
}

// MARK: - Hero

/// Hero 大横幅：精选影片每 8 秒自动轮播，左右滑动手动切换（手动切换后重新计时），右下圆点指示。
/// 整块点按进详情；订阅键与海报卡一致（已订阅切成状态键，点进订阅管理）。
struct DiscoverHero: View {
    let items: [DiscoverPosterItem]
    @State private var index = 0
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        TabView(selection: $index) {
            ForEach(items.indices, id: \.self) { i in
                DiscoverHeroSlide(item: items[i])
                    .tag(i)
            }
        }
        .tabViewStyle(.page(indexDisplayMode: .never))
        .containerRelativeFrame(.vertical) { height, _ in max(height * 0.62, 440) }
        .overlay(alignment: .bottomTrailing) {
            if items.count > 1 {
                HStack(spacing: 6) {
                    ForEach(items.indices, id: \.self) { i in
                        Capsule()
                            .fill(Color.white.opacity(i == index ? 0.85 : 0.3))
                            .frame(width: i == index ? 20 : 6, height: 6)
                            .contentShape(.rect.inset(by: -6))
                            .onTapGesture { withAnimation { index = i } }
                            .accessibilityLabel("切换到《\(items[i].title)》")
                    }
                }
                .padding(.trailing, 20)
                .padding(.bottom, 16)
                .animation(.easeInOut(duration: 0.3), value: index)
            }
        }
        .task(id: "\(index)-\(scenePhase == .active)") {
            // index 作为任务标识：手动切换后重置轮播计时；退到后台不推进
            guard items.count > 1, scenePhase == .active else { return }
            try? await Task.sleep(for: .seconds(8))
            guard !Task.isCancelled else { return }
            withAnimation(.easeInOut(duration: 0.7)) { index = (index + 1) % items.count }
        }
        .accessibilityIdentifier("discover-hero")
    }
}

private struct DiscoverHeroSlide: View {
    let item: DiscoverPosterItem
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router

    private static let shade = Color(red: 7 / 255, green: 9 / 255, blue: 14 / 255)

    var body: some View {
        let sub = SubscriptionIndex.shared.subscription(for: item)
        ZStack(alignment: .bottomLeading) {
            Color.clear
                .overlay { RemoteImage(url: api.image(item.backdropUrl ?? item.posterUrl)) }
                .clipped()
                .mask(LinearGradient(stops: [.init(color: .black, location: 0.55), .init(color: .black.opacity(0.6), location: 0.78), .init(color: .clear, location: 1)], startPoint: .top, endPoint: .bottom))
            LinearGradient(colors: [Self.shade.opacity(0.55), .clear], startPoint: .top, endPoint: UnitPoint(x: 0.5, y: 0.25))
            LinearGradient(colors: [.clear, Self.shade.opacity(0.62)], startPoint: UnitPoint(x: 0.5, y: 0.45), endPoint: .bottom)

            VStack(alignment: .leading, spacing: 6) {
                Text("今日精选 · \(item.mediaType == "tv" ? "剧集" : "电影")")
                    .font(.caption.weight(.semibold))
                    .tracking(2.5)
                    .foregroundStyle(Theme.accent2)
                Text(item.title)
                    .font(.system(size: 30, weight: .bold))
                    .foregroundStyle(.white)
                    .lineLimit(2)
                if !item.originalTitle.isEmpty {
                    Text(item.originalTitle).font(.caption).foregroundStyle(.white.opacity(0.55)).lineLimit(1)
                }
                meta
                if !item.overview.isEmpty {
                    Text(item.overview)
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.75))
                        .lineLimit(2)
                }
                if permissions.canSubscribe {
                    Button {
                        if let sub {
                            router.push(.subscription(id: sub.id))
                        } else {
                            router.present(.subscribe(SubscribeRequest(titleRef: item.resolvedTitleRef, title: item.title)))
                        }
                    } label: {
                        Label(sub == nil ? "订阅影片" : "已订阅", systemImage: sub == nil ? "plus" : "checkmark")
                            .font(.subheadline.weight(.semibold))
                            .padding(.horizontal, 6)
                    }
                    .buttonStyle(HeroButtonStyle(prominent: sub == nil))
                    .padding(.top, 6)
                    .accessibilityLabel(sub == nil ? "订阅影片《\(item.title)》" : "管理《\(item.title)》的订阅")
                    .accessibilityIdentifier("hero-subscribe")
                }
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 28)
            .padding(.trailing, 60)
        }
        .contentShape(.rect)
        .onTapGesture {
            router.push(.mediaDetail(titleRef: item.resolvedTitleRef))
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("查看《\(item.title)》详情")
    }

    private var meta: some View {
        HStack(spacing: 10) {
            if item.rating > 0 {
                HStack(spacing: 3) {
                    Image(systemName: "star.fill").foregroundStyle(Theme.warning)
                    Text(String(format: "%.1f", item.rating)).fontWeight(.semibold).foregroundStyle(.white)
                }
            }
            if let year = item.year { Text(String(year)) }
            if !item.genres.isEmpty { Text(item.genres.joined(separator: " / ")).lineLimit(1) }
            if !item.extent.isEmpty { Text(item.extent) }
            if item.libraryStatus != nil {
                HStack(spacing: 4) {
                    Circle().fill(Theme.success).frame(width: 6, height: 6)
                    Text("在库")
                }
                .foregroundStyle(Color(red: 0.43, green: 0.91, blue: 0.72))
            }
        }
        .font(.subheadline)
        .monospacedDigit()
        .foregroundStyle(.white.opacity(0.8))
    }
}

/// Hero 订阅键：未订阅用强调样式（同 Web btn-accent），已订阅用普通玻璃
private struct HeroButtonStyle: PrimitiveButtonStyle {
    let prominent: Bool

    func makeBody(configuration: Configuration) -> some View {
        if prominent {
            Button(role: configuration.role, action: configuration.trigger) { configuration.label }
                .discoverProminentButton()
        } else {
            Button(role: configuration.role, action: configuration.trigger) { configuration.label }
                .buttonStyle(.glass)
        }
    }
}

struct DiscoverHeroSkeleton: View {
    var body: some View {
        DiscoverSkeletonBlock(cornerRadius: 0)
            .containerRelativeFrame(.vertical) { height, _ in max(height * 0.62, 440) }
            .accessibilityLabel("发现页加载中")
    }
}

// MARK: - 错误态

/// 加载失败：后端中文原因 + 重试；上游不可达（UPSTREAM_UNREACHABLE）额外给「前往网络设置」
struct DiscoverErrorView: View {
    let failure: DiscoverFeed.Failure
    let retry: () async -> Void
    @Environment(Router.self) private var router
    @State private var retrying = false

    var body: some View {
        ContentUnavailableView {
            Label(failure.unreachable ? "无法连接数据源" : "发现页加载失败",
                  systemImage: failure.unreachable ? "globe" : "exclamationmark.triangle")
        } description: {
            Text(failure.message)
        } actions: {
            if failure.unreachable {
                Button("前往网络设置") { router.push(.settingsSection(.network)) }
                    .discoverProminentButton()
                    .accessibilityIdentifier("discover-network-settings")
            }
            Button {
                retrying = true
                Task { await retry(); retrying = false }
            } label: {
                if retrying { ProgressView() } else { Text("重试") }
            }
            .buttonStyle(.glass)
            .disabled(retrying)
        }
        .accessibilityIdentifier("error-state")
    }
}

// MARK: - 院线地区

/// 院线地区就地设置（只在电影 × TMDB 视角出现）：管理员可切换，选择即保存，随后整页重拉
struct DiscoverRegionFooter: View {
    let onChanged: () async -> Void
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var region: String?
    @State private var canEdit = false
    @State private var saved = false

    var body: some View {
        Group {
            if let region {
                HStack(spacing: 6) {
                    Text("院线地区：")
                    if canEdit {
                        Menu {
                            ForEach(DiscoverRegions.all, id: \.code) { item in
                                Button {
                                    Task { await pick(item.code) }
                                } label: {
                                    if item.code == region { Label(item.name, systemImage: "checkmark") } else { Text(item.name) }
                                }
                            }
                        } label: {
                            HStack(spacing: 2) {
                                Text(DiscoverRegions.name(region)).underline()
                                Image(systemName: "chevron.up.chevron.down").font(.caption2)
                            }
                            .foregroundStyle(Theme.textMuted)
                        }
                        .accessibilityIdentifier("discover-region")
                    } else {
                        Text(DiscoverRegions.name(region)).foregroundStyle(Theme.textMuted)
                    }
                    if saved { Text("✓ 已保存").foregroundStyle(Theme.success) }
                }
                .font(.footnote)
                .foregroundStyle(Theme.textFaint)
                .frame(maxWidth: .infinity)
                .padding(.top, 8)
            }
        }
        .task {
            guard region == nil, let view = try? await api.discoverRegionShow() else { return }
            region = view.region
            canEdit = view.canEdit
        }
    }

    private func pick(_ code: String) async {
        guard code != region else { return }
        let previous = region
        region = code // 乐观更新：菜单一收起就看到新地区名
        do {
            let view = try await api.discoverRegionSet(body: .init(region: code))
            region = view.region
            saved = true
            Task {
                try? await Task.sleep(for: .seconds(1.8))
                saved = false
            }
            await onChanged()
        } catch {
            region = previous
            feedback.error(error)
        }
    }
}
