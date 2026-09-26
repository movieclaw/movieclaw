import SwiftUI

/// 订阅海报墙：订阅首页「剧集订阅 ›」「电影订阅 ›」或一排末尾的「查看全部」压栈进来的二级页
/// （`AppRoute.subscriptionWall`）。
///
/// 与首页那一排是**同一份排好的结果**（`SubscriptionsHomeFeed.shared.state(for:)`），不是另算一遍：
/// 顺序、状态签、计数口径两处永远一致。首页横滑只放前 20 张，这里放全部，按首页分隔线的两侧
/// 拆成三段——进行中（此刻最要紧的在前，连没上映的也算）/ 已暂停 / 已收齐（电影叫已入库）。
/// 墙上的海报比首页多一行「规则组 → 媒体库」流向（管理员可见规则组名）。
///
/// 数据：首页刚刷过就直接复用，不重复打接口；从别处直达（或首页数据已旧）时补刷一次。
struct SubscriptionWallView: View {
    let kind: String

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(AppModel.self) private var model

    @State private var ruleSets: [API.RuleSetView] = []
    @State private var libraries: [API.LibraryView] = []

    private var index: SubscriptionIndex { SubscriptionIndex.shared }
    private var feed: SubscriptionsHomeFeed { SubscriptionsHomeFeed.shared }
    private var title: String { kind == "movie" ? "电影订阅" : "剧集订阅" }

    var body: some View {
        let shelf = feed.state(for: index.subscriptions ?? []).shelf(kind)
        ScrollView {
            VStack(alignment: .leading, spacing: 30) {
                Text(summary(shelf))
                    .font(.subheadline)
                    .monospacedDigit()
                    .foregroundStyle(Theme.textMuted)
                    .accessibilityIdentifier("subscriptions-count")
                section("进行中", items: shelf.active, identifier: "wall-active")
                section("已暂停", items: shelf.paused, identifier: "wall-paused")
                section(kind == "movie" ? "已入库" : "已收齐", items: shelf.done, identifier: "wall-done")
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.top, 4)
            .padding(.bottom, 40)
        }
        .scrollIndicators(.hidden)
        .appBackground()
        .navigationTitle(title)
        .refreshable { await refresh(force: true) }
        .task { await refresh(force: false) }
        .task { await loadNames() }
        .tracksSubscriptionIndex()
    }

    @ViewBuilder
    private func section(_ name: String, items: [SubsHomeShelfItem], identifier: String) -> some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Text(name)
                        .font(.title3.weight(.bold))
                        .foregroundStyle(Theme.text)
                    Text("\(items.count)")
                        .font(.subheadline)
                        .monospacedDigit()
                        .foregroundStyle(Theme.textFaint)
                }
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 104), spacing: 12, alignment: .top)],
                    alignment: .leading,
                    spacing: 20
                ) {
                    ForEach(items) { item in
                        SubsHomePosterCard(item: item, flow: flow(for: item.sub), dimsResting: false)
                    }
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier(identifier)
        }
    }

    /// 「共 12 部剧集 · 5 部进行中」：与首页那一排的计数同一口径（进行中 = 分隔线前的那些）
    private func summary(_ shelf: SubsHomeShelf) -> String {
        let total = shelf.all.count
        let active = shelf.active.count
        let noun = kind == "movie" ? "电影" : "剧集"
        guard active > 0, active < total else { return "共 \(total) 部\(noun)" }
        return "共 \(total) 部\(noun) · \(active) 部进行中"
    }

    /// 首页刚刷过就复用；下拉刷新或从别处直达时补刷订阅清单与预告
    private func refresh(force: Bool) async {
        feed.adopt(owner: SubscriptionsHomeFeed.ownerKey(api: api, username: model.session?.username))
        if force {
            _ = await index.refresh(api: api, owner: model.session?.username)
            await feed.refreshAll(api: api, isAdmin: permissions.isAdmin)
        } else {
            await index.ensureLoaded(api: api, owner: model.session?.username)
            await feed.refreshIfStale(api: api, isAdmin: permissions.isAdmin)
        }
    }

    /// 规则组名（管理员可见边界同详情页）与媒体库名：只为拼「规则组 → 库」流向
    private func loadNames() async {
        libraries = (try? await api.libraryList(scope: "all")) ?? []
        ruleSets = permissions.canManageSubscriptions ? ((try? await api.rulesList()) ?? []) : []
    }

    /// 海报第三行：「规则组 → 媒体库」；订阅未指定库时显示该类型默认库
    private func flow(for sub: API.SubscriptionView) -> String? {
        let ruleSet = ruleSets.first { $0.id == sub.ruleSetId }?.name
        let library = sub.libraryId.flatMap { id in libraries.first { $0.id == id }?.name }
            ?? libraries.first { $0.isDefault && $0.kind == sub.media.kind }?.name
        let parts = [ruleSet, library].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " → ")
    }
}
