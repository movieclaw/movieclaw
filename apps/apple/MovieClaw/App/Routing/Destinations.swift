import SwiftUI

/// 路由 → 页面的唯一映射表。
///
/// 每个页面类型的名字和入参在这里固定下来，各模块在自己的目录里实现同名 View；
/// 这样并行开发的模块之间不需要改同一个文件。
extension AppRoute {
    @ViewBuilder
    var destination: some View {
        switch self {
        // 发现
        case let .discover(kind): DiscoverView(kind: kind)
        case let .discoverCollection(kind, provider, collectionId):
            DiscoverCollectionView(kind: kind, provider: provider, collectionId: collectionId)
        case let .mediaDetail(titleRef): MediaDetailView(titleRef: titleRef)
        case let .person(tmdbId): PersonDetailView(tmdbId: tmdbId)
        case let .discoveredPerson(tmdbId): DiscoveredPersonView(tmdbId: tmdbId)
        // 媒体库
        case .libraryHome: LibraryHomeView()
        case .libraryCustomize: LibraryCustomizeView()
        case .favorites: FavoritesView()
        case .allCollections: AllCollectionsView()
        case let .collection(libraryId, collectionId): CollectionDetailView(libraryId: libraryId, collectionId: collectionId)
        case let .library(id, view, pending):
            LibraryDetailView(libraryId: id, initialView: view.flatMap(LibraryDetailView.WallView.init(rawValue:)) ?? .items, openPending: pending)
        case let .libraryItem(libraryId, itemId, season, episode):
            LibraryItemDetailView(libraryId: libraryId, itemId: itemId, season: season, episode: episode)
        case let .libraryManage(create, tab, item): LibraryManageView(openCreate: create, initialTab: tab, initialItemId: item)
        // 搜索
        case let .searchHome(mode): SearchHomeView(initialMode: mode)
        case let .search(query): SearchResultsView(query: query)
        // 订阅
        case .subscriptions: SubscriptionsView()
        case let .subscription(id, upgradeRun): SubscriptionDetailView(subscriptionId: id, openUpgradeRun: upgradeRun)
        // 活动
        case .activity: ActivityView()
        case let .activityPage(page): ActivityPageView(page: page)
        // AI 会话
        case .newSession: AgentNewSessionView()
        case let .session(id): AgentConversationView(sessionId: id)
        // 我的 / 设置
        case .my: MorePage()
        case .settings: SettingsIndexView()
        case let .settingsSection(section, query): SettingsSectionView(section: section).environment(\.routeQuery, query)
        // 分享
        case let .share(slug): SharePageView(slug: slug)
        }
    }
}

extension AppSheet {
    @ViewBuilder
    var content: some View {
        switch self {
        case let .subscribe(request): SubscribeSheet(request: request)
        case .accountSwitcher: AccountSwitcherSheet()
        }
    }
}
