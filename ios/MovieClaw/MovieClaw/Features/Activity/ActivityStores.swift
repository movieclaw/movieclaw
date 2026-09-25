import SwiftUI
import UIKit

/// 全 App 唯一的「任务活动」数据源（对应 Web `JobsProvider` + `DownloadTasksProvider` + `useTaskActivity`）。
///
/// 设计要点：
/// - **一份数据两处用**：外壳活动标签的角标与活动页任务视角读同一个实例（挂在 `ShellBadges.tasks` 上），
///   避免两条 SSE、两路下载器轮询，也保证角标数与页面选项卡的数字永远一致；
/// - **Job**：首次取快照，之后 `GET /jobs/stream` 的 `ready`/`job` 事件触发刷新（120ms 防抖合并连发事件），
///   反代不支持流式或断线时由 15 秒兜底轮询补齐；回到前台立即刷新；SSE 断开 3 秒后自动重连；
/// - 刷新请求串行排空：进行中时再来的通知只记一次「待补」，避免重叠查询风暴；
///   活跃任务（最多 200）与最近 50 条并行取再合并，运行很久的旧任务不会被新完成的记录挤掉；
/// - **下载任务**：10 秒轮询 `GET /downloaders/tasks`（下载器是外部服务，只读这一份快照）；
/// - 归类与计数交给纯函数 `TaskCenter.buildActivity`，每次数据变化重算一次缓存在 `activity`。
@Observable
final class TaskActivityStore {
    private(set) var jobs: [API.JobView] = []
    private(set) var downloads: [API.DownloadTaskView] = []
    private(set) var sources: [API.DownloadTaskSourceView] = []
    /// 归类结果（角标、选项卡计数、各分区都读它）
    private(set) var activity = TaskCenter.Activity()
    private(set) var downloadsLoading = true
    private(set) var downloadsError: String?
    private(set) var downloadsRefreshedAt: Date?
    private(set) var jobsLoaded = false
    /// SSE 实时通道是否在线、收到的事件数（任务页右上角的「实时」指示与验收用）
    private(set) var streamConnected = false
    private(set) var streamEventCount = 0

    @ObservationIgnored private var api: APIClient?
    @ObservationIgnored private var jobsInFlight = false
    @ObservationIgnored private var jobsQueued = false
    @ObservationIgnored private var jobsGeneration = 0
    @ObservationIgnored private var debounce: Task<Void, Never>?
    @ObservationIgnored private var downloadsInFlight = false

    static let fallbackPoll: Duration = .seconds(15)
    static let downloadsPoll: Duration = .seconds(10)

    /// 由外壳在管理员登录期间常驻运行；取消即全部停止（含 SSE 连接）
    func run(api: APIClient) async {
        self.api = api
        jobs = []
        downloads = []
        sources = []
        activity = TaskCenter.Activity()
        jobsLoaded = false
        downloadsLoading = true
        refreshJobs()
        refreshDownloads()
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.streamLoop(api) }
            group.addTask { await self.repeatEvery(Self.fallbackPoll) { self.refreshJobs() } }
            group.addTask { await self.repeatEvery(Self.downloadsPoll) { self.refreshDownloads() } }
            group.addTask { await self.onForeground { self.refreshJobs(); self.refreshDownloads() } }
        }
        self.api = nil
        streamConnected = false
    }

    /// 立即重新拉取 Job 快照（写操作后校准、下拉刷新）
    func refreshJobs() {
        jobsQueued = true
        guard !jobsInFlight, let api else { return }
        jobsInFlight = true
        Task {
            defer { jobsInFlight = false }
            while jobsQueued {
                jobsQueued = false
                jobsGeneration += 1
                let generation = jobsGeneration
                do {
                    async let active = api.jobsList(activeOnly: true, limit: 200)
                    async let recent = api.jobsList(limit: 50)
                    let (activeList, recentList) = try await (active, recent)
                    guard generation == jobsGeneration else { continue }
                    var merged: [String: API.JobView] = [:]
                    for job in recentList.items + activeList.items { merged[job.id] = job }
                    setJobs(Array(merged.values))
                    jobsLoaded = true
                } catch {
                    // 瞬时断线保留最近快照；SSE 重连或下一轮兜底轮询会自动校准
                }
            }
        }
    }

    /// 立即重新读取下载器快照
    func refreshDownloads() {
        guard !downloadsInFlight, let api else { return }
        downloadsInFlight = true
        Task {
            defer { downloadsInFlight = false; downloadsLoading = false }
            do {
                let snapshot = try await api.dlTasks()
                downloads = snapshot.items
                sources = snapshot.sources
                downloadsError = nil
                downloadsRefreshedAt = .now
                rebuild()
            } catch is CancellationError {
            } catch {
                downloadsError = error.localizedDescription.isEmpty ? "下载任务加载失败" : error.localizedDescription
            }
        }
    }

    /// 写操作返回的最新 Job 直接并入（不等下一轮刷新）
    func upsert(_ job: API.JobView) {
        setJobs([job] + jobs.filter { $0.id != job.id })
    }

    private func setJobs(_ list: [API.JobView]) {
        // 按更新时间倒序（先解析一次时间再排，避免比较时反复解析）
        jobs = list
            .map { ($0, Formatters.date($0.updatedAt) ?? .distantPast) }
            .sorted { $0.1 > $1.1 }
            .map(\.0)
        rebuild()
    }

    private func rebuild() {
        activity = TaskCenter.buildActivity(jobs: jobs, downloads: downloads)
    }

    // MARK: 循环

    private func streamLoop(_ api: APIClient) async {
        while !Task.isCancelled {
            do {
                for try await event in api.events("/jobs/stream") {
                    streamConnected = true
                    if event.event == "ready" || event.event == "job" {
                        streamEventCount += 1
                        scheduleRefreshFromEvent()
                    }
                }
            } catch {
                // 断线（后台挂起、反代超时、服务器重启）：稍后重连，期间由兜底轮询保证数据不停
            }
            streamConnected = false
            try? await Task.sleep(for: .seconds(3))
        }
    }

    /// 服务端一次可能连推多条事件，合为一次快照校准（Web 同为 120ms）
    private func scheduleRefreshFromEvent() {
        guard debounce == nil else { return }
        debounce = Task {
            try? await Task.sleep(for: .milliseconds(120))
            debounce = nil
            refreshJobs()
        }
    }

    private func repeatEvery(_ interval: Duration, _ action: @escaping @MainActor () -> Void) async {
        while !Task.isCancelled {
            try? await Task.sleep(for: interval)
            if Task.isCancelled { break }
            // App 在后台时系统会挂起进程；这里再挡一下，避免回前台瞬间补发一串请求
            if UIApplication.shared.applicationState == .active { action() }
        }
    }

    private func onForeground(_ action: @escaping @MainActor () -> Void) async {
        for await _ in NotificationCenter.default.notifications(named: UIApplication.didBecomeActiveNotification).map({ _ in () }) {
            action()
        }
    }
}

/// 媒体库实时活动（谁在看什么）：8 秒轮询 `GET /playback/activity`（对应 Web `useMediaActivity`）。
///
/// 外壳活动标签的「有人在看」提示与活动页「正在播放」共用这一份：Web 是两处各自轮询，
/// App 里合成一路，数据一致也省一半请求。可见范围口径（我的浏览范围 / 全部）记在本机。
@Observable
final class MediaActivityStore {
    private(set) var snapshot = API.MediaActivityView(sessions: [], downloads: [], hiddenSessionCount: 0, hiddenDownloadCount: 0)
    private(set) var loading = true
    private(set) var error: String?
    private(set) var scope: String = MediaActivityStore.loadScope()

    @ObservationIgnored private var api: APIClient?
    @ObservationIgnored private var inFlight = false
    @ObservationIgnored private var loaded = false

    static let poll: Duration = .seconds(8)
    private static let scopeKey = "movieclaw.activity.scope"

    /// 「此刻有人在播」的计数口径：范围外折叠的会话与下载也算
    var liveCount: Int {
        snapshot.sessions.count + snapshot.downloads.count + snapshot.hiddenSessionCount + snapshot.hiddenDownloadCount
    }

    func run(api: APIClient) async {
        self.api = api
        loaded = false
        refresh()
        await withTaskGroup(of: Void.self) { group in
            group.addTask {
                while !Task.isCancelled {
                    try? await Task.sleep(for: Self.poll)
                    if Task.isCancelled { break }
                    await MainActor.run { if UIApplication.shared.applicationState == .active { self.refresh() } }
                }
            }
            group.addTask {
                for await _ in NotificationCenter.default.notifications(named: UIApplication.didBecomeActiveNotification).map({ _ in () }) {
                    await MainActor.run { self.refresh() }
                }
            }
        }
        self.api = nil
    }

    func refresh() {
        guard !inFlight, let api else { return }
        inFlight = true
        if !loaded { loading = true }
        let target = scope
        Task {
            defer { inFlight = false; loading = false }
            do {
                let next = try await api.playbackActivity(scope: target)
                // 切口径期间在途的旧口径响应不能盖掉新口径
                guard target == scope else { return }
                snapshot = next
                error = nil
                loaded = true
            } catch is CancellationError {
            } catch {
                self.error = error.localizedDescription.isEmpty ? "媒体库活动加载失败" : error.localizedDescription
            }
        }
    }

    /// 切换可见范围：记住选择并立即按新口径重拉
    func setScope(_ next: String) {
        guard next != scope else { return }
        scope = next
        if next == "all" {
            UserDefaults.standard.set(next, forKey: Self.scopeKey)
        } else {
            UserDefaults.standard.removeObject(forKey: Self.scopeKey)
        }
        loaded = false
        inFlight = false
        refresh()
    }

    private static func loadScope() -> String {
        UserDefaults.standard.string(forKey: scopeKey) == "all" ? "all" : "visible"
    }
}
