import Foundation
import Observation

/// 站点资源搜索页的状态机（对应 Web `SearchResults` 组件的状态部分）。
///
/// - 实时搜索：消费 SSE 流，`start` 建立逐站进度，`site_result` 追加结果、`site_error` 记录失败原因，
///   `done` 汇总总耗时；流本身失败（网络/鉴权）才进入整页错误态，单站失败不算。
/// - 快照回放：读历史留存的完整结果，不打站点；页头给出快照年龄与「重新搜索」。
/// - 单站重试、加载更多（各站独立分页，全部站点都没带来新条目即视为到底）。
/// - 筛选/排序/视图全部是纯前端操作；每次结果或条件变化统一重算派生数据（见 `recompute`）。
///
/// 模型由页面 `@State` 持有：切换「影视 / 站点资源 / 媒体库」只切显示，流式搜索照常进行、结果保留；
/// 离开页面模型释放，`deinit` 取消所有在途请求。
@Observable
final class TorrentSearchModel {
    enum Phase: Equatable { case idle, connecting, streaming, done, error }

    struct SiteProgress: Identifiable, Hashable {
        enum State: Hashable { case searching, ok, error }
        var siteId: String
        var siteName: String
        var state: State
        var count = 0
        var error: String?
        var elapsedMs: Int?
        var id: String { siteId }
    }

    let keyword: String
    let scope: SearchScope
    let snapshotId: Int?

    private(set) var phase: Phase = .idle
    private(set) var fatalError: String?
    private(set) var items: [API.TorrentHit] = []
    private(set) var sites: [SiteProgress] = []
    private(set) var totalElapsedMs: Int?
    private(set) var snapshotAt: String?
    private(set) var page = 1
    private(set) var loadingMore = false
    private(set) var exhausted = false

    var filters = TorrentFilters() { didSet { recompute() } }
    var sort = TorrentSort() { didSet { recompute() } }
    var view: TorrentResultView

    // 派生数据（结果或条件变化时统一重算，避免每次渲染都遍历上千条结果）
    private(set) var facets = TorrentFacets()
    private(set) var filtered: [API.TorrentHit] = []
    private(set) var sorted: [API.TorrentHit] = []
    private(set) var entities: [String: TorrentEntity] = [:]
    private(set) var smartSortKeys: [TorrentSortKey] = [.free]

    @ObservationIgnored private var mainTask: Task<Void, Never>?
    @ObservationIgnored private var auxTasks: [UUID: Task<Void, Never>] = [:]

    init(keyword: String, scope: SearchScope, snapshotId: Int?) {
        self.keyword = keyword
        self.scope = scope
        self.snapshotId = snapshotId
        view = scope.posterMode ? .poster : .group
    }

    isolated deinit {
        mainTask?.cancel()
        for task in auxTasks.values { task.cancel() }
    }

    var streaming: Bool { phase == .connecting || phase == .streaming }
    var settledCount: Int { sites.filter { $0.state != .searching }.count }
    var failedCount: Int { sites.filter { $0.state == .error }.count }
    var okSites: [SiteProgress] { sites.filter { $0.state == .ok } }

    func siteName(_ id: String) -> String { sites.first { $0.siteId == id }?.siteName ?? id }

    /// 首次切到「站点资源」才调用（惰性：默认落在影视时不打扰任何 PT 站点）
    func start(api: APIClient) {
        guard phase == .idle else { return }
        phase = .connecting
        if let snapshotId {
            mainTask = Task { await loadSnapshot(api: api, id: snapshotId) }
        } else {
            mainTask = Task { await stream(api: api) }
        }
    }

    private func loadSnapshot(api: APIClient, id: Int) async {
        do {
            let snap = try await api.torrentSearchSnapshot(historyId: id)
            items = snap.items
            sites = snap.sites.map {
                SiteProgress(siteId: $0.siteId, siteName: $0.siteName, state: $0.error == nil ? .ok : .error, count: $0.count, error: $0.error, elapsedMs: $0.elapsedMs)
            }
            totalElapsedMs = snap.elapsedMs
            snapshotAt = snap.snapshotAt
            phase = .done
            recompute()
        } catch is CancellationError {
        } catch {
            phase = .error
            fatalError = error.localizedDescription.isEmpty ? "快照加载失败，请稍后重试" : error.localizedDescription
        }
    }

    private func stream(api: APIClient) async {
        do {
            for try await event in api.torrentSearchStream(keyword: keyword, scope: scope) {
                switch event {
                case let .start(list):
                    phase = .streaming
                    sites = list.map { SiteProgress(siteId: $0.siteId, siteName: $0.siteName, state: .searching) }
                case .siteStart:
                    break
                case let .siteResult(siteId, _, count, elapsed, hits):
                    items += hits
                    patch(siteId) { $0.state = .ok; $0.count = count; $0.elapsedMs = elapsed }
                    recompute()
                case let .siteError(siteId, _, error, elapsed):
                    patch(siteId) { $0.state = .error; $0.error = error; $0.elapsedMs = elapsed }
                case let .done(_, elapsed, _):
                    totalElapsedMs = elapsed
                    phase = .done
                }
            }
            if phase != .done { phase = .done }
        } catch is CancellationError {
        } catch {
            if Task.isCancelled { return }
            phase = .error
            fatalError = error.localizedDescription.isEmpty ? "搜索失败，请稍后重试" : error.localizedDescription
        }
    }

    private func patch(_ siteId: String, _ change: (inout SiteProgress) -> Void) {
        guard let i = sites.firstIndex(where: { $0.siteId == siteId }) else { return }
        change(&sites[i])
    }

    /// 单站重试：清掉该站旧结果，只对它重发一次流（只搜第 1 页）
    func retrySite(_ siteId: String, api: APIClient) {
        items.removeAll { $0.siteId == siteId }
        patch(siteId) { $0.state = .searching; $0.count = 0; $0.error = nil; $0.elapsedMs = nil }
        recompute()
        var single = scope
        single.siteIds = [siteId]
        runAux { [weak self] in
            do {
                for try await event in api.torrentSearchStream(keyword: self?.keyword ?? "", scope: single) {
                    guard let self else { return }
                    switch event {
                    case let .siteResult(id, _, count, elapsed, hits):
                        items += hits
                        patch(id) { $0.state = .ok; $0.count = count; $0.error = nil; $0.elapsedMs = elapsed }
                        recompute()
                    case let .siteError(id, _, error, elapsed):
                        patch(id) { $0.state = .error; $0.error = error; $0.elapsedMs = elapsed }
                    default:
                        break
                    }
                }
            } catch {}
            // 流结束（或失败）时该站仍无结论：按失败处理
            self?.patch(siteId) { if $0.state == .searching { $0.state = .error; $0.error = "重试失败，请稍后再试" } }
        }
    }

    /// 加载更多：以 page+1 重发一次全范围流，新结果去重后追加；全部站点都没带来新条目 → 到底
    func loadMore(api: APIClient) {
        guard !loadingMore, !exhausted, phase == .done, snapshotAt == nil else { return }
        let next = page + 1
        loadingMore = true
        runAux { [weak self] in
            var added = 0
            var completed = false
            do {
                for try await event in api.torrentSearchStream(keyword: self?.keyword ?? "", scope: self?.scope ?? .all, page: next) {
                    guard let self else { return }
                    if case let .siteResult(_, _, _, _, hits) = event {
                        let seen = Set(items.map { "\($0.siteId)/\($0.torrentId)" })
                        let fresh = hits.filter { !seen.contains("\($0.siteId)/\($0.torrentId)") }
                        added += fresh.count
                        if !fresh.isEmpty {
                            items += fresh
                            recompute()
                        }
                    }
                }
                completed = true
            } catch {}
            guard let self else { return }
            loadingMore = false
            if completed {
                page = next
                if added == 0 { exhausted = true }
            }
        }
    }

    private func runAux(_ body: @escaping () async -> Void) {
        let id = UUID()
        auxTasks[id] = Task { [weak self] in
            await body()
            self?.auxTasks[id] = nil
        }
    }

    /// 重算派生数据（筛选 → 排序 → 分面 → 作品分组 → 智能排序键）
    func recompute() {
        let f = filters
        filtered = items.filter { TorrentSearchLogic.matches($0, f) }
        sorted = TorrentSearchLogic.sorted(filtered, by: sort)
        facets = TorrentSearchLogic.facets(items, filters: f)
        entities = TorrentSearchLogic.entities(items)
        smartSortKeys = TorrentSearchLogic.smartSortKeys(items)
    }
}
