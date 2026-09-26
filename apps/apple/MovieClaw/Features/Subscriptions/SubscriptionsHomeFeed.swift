import SwiftUI

/// 订阅首页与「全部」海报墙共用的数据源，以及排好的整页结果。
///
/// 为什么是一个共享对象而不是页面各自的 @State：
/// - 海报墙必须和首页那一排讲同一套顺序与状态签（从首页点「›」进去，顺序不能变），
///   两页读的是同一份算好的结果，口径不可能分叉；
/// - 海报墙不必为此再打一遍预告 / 刚刚入库 / 下载快照三个接口，首页取到的直接复用；
/// - 排序只在数据真的变化时算一次：Hero 每 8 秒换一张、页面底色交叉淡入都会让首页重算 body，
///   `state(for:)` 按输入指纹命中缓存，订阅再多也不会每次重排。
///
/// 单例跨账号存活，所以记着属于谁（服务器地址 + 用户名，同 SubscriptionIndex）：换账号即清空，
/// 刷新期间换了账号的迟到结果直接丢弃，不串到别的账号上。
@MainActor
@Observable
final class SubscriptionsHomeFeed {
    static let shared = SubscriptionsHomeFeed()

    /// 整周预告；nil = 还没取到（与「取到了但一周都没安排」的空数组区分）
    private(set) var week: [API.TodayArrivalView]?
    private(set) var recent: [API.RecentArrivalView] = []
    private(set) var tasks: [API.DownloadTaskView] = []
    /// 算「几点能看」「多久前入库」用的当前时刻，随预告一起刷新
    private(set) var now = Date()

    @ObservationIgnored private var owner: String?
    @ObservationIgnored private var refreshedAt: Date?
    @ObservationIgnored private var cache: (key: Int, state: SubsHomeState)?

    static func ownerKey(api: APIClient, username: String?) -> String {
        "\(api.server.apiBase.absoluteString)|\(username ?? "")"
    }

    /// 以这个账号的身份使用：换了账号就清掉旧账号的数据与缓存
    func adopt(owner key: String) {
        guard key != owner else { return }
        owner = key
        week = nil
        recent = []
        tasks = []
        refreshedAt = nil
        cache = nil
    }

    /// 算好的整页结果。输入（订阅清单、预告、刚刚入库、下载快照、时刻）没变就直接复用上一次
    func state(for subscriptions: [API.SubscriptionView]) -> SubsHomeState {
        var hasher = Hasher()
        hasher.combine(subscriptions)
        hasher.combine(week)
        hasher.combine(recent)
        hasher.combine(tasks)
        hasher.combine(now)
        let key = hasher.finalize()
        if let cache, cache.key == key { return cache.state }
        let state = SubscriptionsHome.state(
            subscriptions: subscriptions, week: week ?? [], recent: recent, tasks: tasks, now: now
        )
        cache = (key, state)
        return state
    }

    // MARK: 刷新

    /// 海报墙打开时用：首页刚刷过就不再打接口（首页自己有 10 秒轮询）
    func refreshIfStale(api: APIClient, isAdmin: Bool, maxAge: TimeInterval = 30) async {
        if let refreshedAt, Date.now.timeIntervalSince(refreshedAt) < maxAge { return }
        await refreshAll(api: api, isAdmin: isAdmin)
    }

    func refreshAll(api: APIClient, isAdmin: Bool) async {
        async let arrivals: Void = refreshArrivals(api: api)
        async let arrived: Void = refreshRecent(api: api)
        async let snapshot: Void = refreshTasks(api: api, isAdmin: isAdmin)
        _ = await (arrivals, arrived, snapshot)
    }

    /// 整周预告：已有快照时瞬时失败继续保留，不闪成空
    func refreshArrivals(api: APIClient) async {
        let key = owner
        do {
            let list = try await api.subscriptionsListTodayArrivals(window: "week")
            guard key == owner else { return }
            week = list
            now = .now
            refreshedAt = .now
        } catch is CancellationError {
        } catch {
            if key == owner, week == nil { week = [] }
        }
    }

    /// 刚刚入库：老版本服务端没有这个接口（404）或瞬时失败时保持原样，这一行不出现 / 不闪
    func refreshRecent(api: APIClient) async {
        let key = owner
        if let list = try? await api.subscriptionsListRecentArrivals(), key == owner {
            recent = list
        }
    }

    /// 下载任务快照（管理员）：「下载中」的进度与预计时间用下载器实时数据修正
    func refreshTasks(api: APIClient, isAdmin: Bool) async {
        guard isAdmin else { return }
        let key = owner
        if let list = try? await api.dlTasks(), key == owner {
            tasks = list.items
        }
    }
}
