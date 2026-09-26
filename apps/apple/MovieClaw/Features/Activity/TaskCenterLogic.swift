import SwiftUI

/// 任务中心的纯逻辑（无 UI、无请求，可单测）。逐条移植自 Web：
/// - `lib/job-attention.ts`、`lib/download-attention.ts`、`lib/task-activity.ts`（归类与**唯一计数口径**）；
/// - `components/task-center-view.tsx` / `job-center.tsx` 里的文案与状态推导函数；
/// - `lib/episode-units.ts`（季集摘要）、`lib/ingest-history.ts`（入库历史明细）。
///
/// 为什么集中成一个命名空间：外壳角标、活动页一级切换器、任务视角的状态选项卡必须读
/// 同一份判定，各算各的就会出现「角标 3、进行中 2」的自相矛盾（Web 的真实教训）。
/// 所有类型嵌套在 `TaskCenter` 下，避免与并行开发的其它模块撞名。
enum TaskCenter {}

// MARK: - JSON 小工具（Job 的 details / input_data / error 都是任意 JSON）

extension TaskCenter {
    /// 只认真正的字符串（Web `typeof x === "string" && x`），不把数字转成串
    static func string(_ dict: [String: API.JSONValue]?, _ key: String) -> String? {
        if case let .string(value)? = dict?[key], !value.isEmpty { return value }
        return nil
    }

    static func number(_ dict: [String: API.JSONValue]?, _ key: String) -> Double? {
        switch dict?[key] {
        case let .int(value)?: Double(value)
        case let .double(value)?: value.isFinite ? value : nil
        default: nil
        }
    }

    static func strings(_ dict: [String: API.JSONValue]?, _ key: String) -> [String] {
        guard case let .array(items)? = dict?[key] else { return [] }
        var seen = Set<String>()
        return items.compactMap { item in
            guard case let .string(value) = item, !value.isEmpty, seen.insert(value).inserted else { return nil }
            return value
        }
    }

    /// 数组长度或数字（Web `detailCount`）
    static func count(_ dict: [String: API.JSONValue]?, _ key: String) -> Int {
        switch dict?[key] {
        case let .array(items)?: items.count
        case let .int(value)?: value
        case let .double(value)?: Int(value)
        default: 0
        }
    }
}

// MARK: - 状态判定（job-attention / download-attention）

extension TaskCenter {
    /// 需要用户判断的 Job 状态；与「需要处理」同口径
    static let attentionJobStatuses: Set<String> = ["blocked", "failed"]
    /// 「现在」时间线里的进行中状态
    static let activeFeedJobStatuses: Set<String> = ["queued", "running", "retry_wait", "cancelling", "waiting"]
    static let historyJobStatuses: Set<String> = ["succeeded", "cancelled"]
    /// 仍占着资源的活跃状态（含 blocked），拉取「活跃任务」时的口径
    static let activeJobStatuses: Set<String> = activeFeedJobStatuses.union(["blocked"])

    /// 失败任务是否已被用户忽略
    static func isDismissed(_ job: API.JobView) -> Bool { job.dismissedAt != nil }

    /// 这条 Job 是不是**现在**要用户动手：失败但已忽略的不算（issue #221 的出口）
    static func jobNeedsAttention(_ job: API.JobView) -> Bool {
        attentionJobStatuses.contains(job.status) && !isDismissed(job)
    }

    /// 已经了结的 Job：正常终态，加上被忽略的失败任务
    static func jobIsHistorical(_ job: API.JobView) -> Bool {
        historyJobStatuses.contains(job.status) || (job.status == "failed" && isDismissed(job))
    }

    /// 系统自动收口的取消（`system:` 前缀）：重跑没有意义，不给「重新执行」
    static func isSystemCancelled(_ job: API.JobView) -> Bool {
        job.status == "cancelled" && (job.cancelRequestedBy?.hasPrefix("system:") ?? false)
    }

    /// 内容核验证明种子里没有的集（「S01E05」或「S01E05 等 3 集」）
    static func contentMissingLabel(_ task: API.DownloadTaskView) -> String? {
        let units = (task.subscriptions.first?.units ?? []).filter(\.contentMissing)
        guard let first = units.first else { return nil }
        let label = "S\(pad2(first.seasonNumber))E\(pad2(first.episodeNumber))"
        return units.count == 1 ? label : "\(label) 等 \(units.count) 集"
    }

    /// 下载任务是否要用户处理。外部任务（非 MovieClaw 投递）只观察不报警。
    static func downloadTaskNeedsAttention(_ task: API.DownloadTaskView, ingestJob: API.JobView?) -> Bool {
        if task.source == "external" { return false }
        return task.canReplace
            || task.state == "error"
            || task.state == "missing"
            || task.landingError != nil
            || contentMissingLabel(task) != nil
            || (ingestJob.map(jobNeedsAttention) ?? false)
    }

    /// 只有进入救援窗口且仍能定位下载器任务时，才提供「立即换种」
    static func shouldOfferInlineReplacement(_ task: API.DownloadTaskView) -> Bool {
        task.canReplace && task.downloaderId != nil
    }

    static func pad2(_ value: Int) -> String { String(format: "%02d", value) }
}

// MARK: - 分组与计数（task-activity）

extension TaskCenter {
    /// 同一部作品的多个下载资源合并为一组（只按媒体条目主键合并，未识别的各自一组）
    struct DownloadGroup: Identifiable {
        var key: String
        var mediaItemId: Int?
        var title: String
        var kind: String?
        var posterUrl: String?
        var tasks: [API.DownloadTaskView]
        var id: String { key }
    }

    static func groupDownloadTasks(_ tasks: [API.DownloadTaskView]) -> [DownloadGroup] {
        var groups: [DownloadGroup] = []
        var index: [String: Int] = [:]
        for task in tasks {
            let key = task.mediaItemId.map { "media:\($0)" } ?? "task:\(task.id)"
            if let position = index[key] {
                groups[position].tasks.append(task)
                if groups[position].posterUrl == nil, let poster = task.posterUrl { groups[position].posterUrl = poster }
                continue
            }
            index[key] = groups.count
            groups.append(DownloadGroup(
                key: key, mediaItemId: task.mediaItemId,
                title: nonEmpty(task.mediaTitle) ?? nonEmpty(task.name) ?? task.infoHash,
                kind: task.mediaKind, posterUrl: task.posterUrl, tasks: [task]
            ))
        }
        return groups
    }

    static func groupNeedsAttention(_ group: DownloadGroup, ingestJobsByHash: [String: API.JobView]) -> Bool {
        group.tasks.contains { downloadTaskNeedsAttention($0, ingestJob: ingestJobsByHash[$0.infoHash.lowercased()]) }
    }

    /// 任务活动的归类结果。外壳角标、活动页切换器、任务视角的选项卡都读它。
    struct Activity {
        /// infohash（小写）→ 关联的入库 Job：下载与入库是同一件事的两段
        var ingestJobsByHash: [String: API.JobView] = [:]
        var attentionDownloadGroups: [DownloadGroup] = []
        var activeDownloadGroups: [DownloadGroup] = []
        /// 刷流做种：没有入库/救援语义，不进时间线、不参与计数
        var boostTasks: [API.DownloadTaskView] = []
        var standaloneAttentionJobs: [API.JobView] = []
        var standaloneActiveJobs: [API.JobView] = []
        var standaloneHistoricalJobs: [API.JobView] = []

        var attentionTotal: Int { attentionDownloadGroups.count + standaloneAttentionJobs.count }
        var activeTotal: Int { activeDownloadGroups.count + standaloneActiveJobs.count }
        var historyTotal: Int { standaloneHistoricalJobs.count }
    }

    /// 汇总任务活动（Web `useTaskActivity`）。jobs 需已按更新时间倒序。
    static func buildActivity(jobs: [API.JobView], downloads: [API.DownloadTaskView]) -> Activity {
        var result = Activity()
        for job in jobs where job.jobType == "library.ingest" {
            for resource in job.resources where resource.resourceType == "download" {
                let key = resource.resourceId.lowercased()
                if result.ingestJobsByHash[key] == nil { result.ingestJobsByHash[key] = job }
            }
        }
        result.boostTasks = downloads.filter { $0.source == "boost" }
        let groups = groupDownloadTasks(downloads.filter { $0.source != "boost" })
        for group in groups {
            if groupNeedsAttention(group, ingestJobsByHash: result.ingestJobsByHash) {
                result.attentionDownloadGroups.append(group)
            } else {
                result.activeDownloadGroups.append(group)
            }
        }
        // 已串进下载生命周期的入库 Job 不再作为第二件事重复出现（既不渲染也不计数）
        let linked = Set(downloads.compactMap { result.ingestJobsByHash[$0.infoHash.lowercased()]?.id })
        for job in jobs where !linked.contains(job.id) {
            if jobNeedsAttention(job) { result.standaloneAttentionJobs.append(job) }
            if activeFeedJobStatuses.contains(job.status) { result.standaloneActiveJobs.append(job) }
            if jobIsHistorical(job) { result.standaloneHistoricalJobs.append(job) }
        }
        return result
    }

    /// 角标展示模型：有需要处理时警示（红），否则进行中（蓝）；count 为 0 表示不出角标
    struct Badge: Equatable {
        var alert: Bool
        var count: Int
        var hint: String
    }

    static func badge(_ activity: Activity) -> Badge {
        if activity.attentionTotal > 0 {
            return Badge(alert: true, count: activity.attentionTotal, hint: "\(activity.attentionTotal) 项需要处理")
        }
        let active = activity.activeTotal
        return Badge(alert: false, count: active, hint: active > 0 ? "\(active) 个进行中" : "暂无进行中的任务")
    }

    static func nonEmpty(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }
}

// MARK: - 文案表（job-center.tsx / task-center-view.tsx）

extension TaskCenter {
    static let jobTypeLabels: [String: String] = [
        "subtitle.generate": "生成 AI 字幕",
        "library.scan": "扫描媒体库",
        "library.metadata.refresh": "刷新媒体库元数据",
        "media.metadata.refresh": "刷新条目元数据",
        "library.chapter_images": "生成章节",
        "library.organize": "整理媒体库文件",
        "library.transfer": "转移媒体库条目",
        "library.ingest": "自动整理入库",
    ]

    static let jobStatusLabels: [String: String] = [
        "queued": "排队中", "running": "进行中", "retry_wait": "等待重试", "cancelling": "正在取消",
        "waiting": "等待前置任务", "blocked": "需要处理", "succeeded": "已完成", "failed": "未完成", "cancelled": "已取消",
    ]

    static let jobPhaseLabels: [String: String] = [
        "queued": "等待执行", "preparing": "准备", "ocr": "识别图形字幕", "syncing": "校准时间轴",
        "glossary": "整理术语", "translating": "翻译字幕", "validating": "检查译文", "compressing": "压缩字幕",
        "writing": "写入文件", "refreshing": "刷新元数据", "walking": "遍历文件", "ingesting": "识别入账",
        "probing": "分析媒体", "assets": "补齐资产", "organizing": "整理文件", "transferring": "搬运文件",
        "identifying": "识别条目", "waiting_library": "等待媒体库", "waiting_stable": "等待下载稳定",
        "finalizing": "收尾", "retry_wait": "等待重试", "blocked": "等待处理", "completed": "已完成", "cancelled": "已取消",
    ]

    static let languageLabels: [String: String] = [
        "chs": "简体中文", "cht": "繁体中文", "eng": "英语", "jpn": "日语", "kor": "韩语", "fre": "法语",
        "ger": "德语", "spa": "西班牙语", "ita": "意大利语", "por": "葡萄牙语", "rus": "俄语", "tha": "泰语",
    ]

    static let activeJobActions: [String: String] = [
        "subtitle.generate": "正在生成字幕", "library.scan": "正在扫描", "library.metadata.refresh": "正在刷新媒体库元数据",
        "media.metadata.refresh": "正在刷新元数据", "library.chapter_images": "正在生成章节", "library.organize": "正在整理文件",
        "library.transfer": "正在转移文件", "library.ingest": "正在入库",
    ]

    static let completedJobActions: [String: String] = [
        "subtitle.generate": "字幕生成", "library.scan": "扫描", "library.metadata.refresh": "元数据刷新",
        "media.metadata.refresh": "元数据刷新", "library.chapter_images": "章节生成", "library.organize": "文件整理",
        "library.transfer": "文件转移", "library.ingest": "入库",
    ]

    /// 有自动来源的任务类型：忽略时可以连来源一起静音
    static let autoRecreatedJobTypes: [String: String] = [
        "subtitle.generate": "不再自动为这个文件生成字幕",
    ]

    /// Job 卡片错误动作里 App 能执行的类型
    static let supportedErrorActions: Set<String> = ["retry_job", "open_settings", "inspect_logs", "update_runtime", "handoff_agent"]

    static let neutral = Color(red: 0xCB / 255, green: 0xD5 / 255, blue: 0xE1 / 255)

    /// 一个状态的展示三件套：文字、颜色（进度条与状态点共用）、是否呼吸
    struct StateMeta {
        var label: String
        var color: Color
        var pulse: Bool = false
    }

    static func downloadStateMeta(_ state: String) -> StateMeta {
        switch state {
        case "downloading": StateMeta(label: "下载中", color: Theme.info, pulse: true)
        case "stalled": StateMeta(label: "等待连接", color: Theme.warning)
        case "paused": StateMeta(label: "已暂停", color: Theme.warning)
        case "queued": StateMeta(label: "排队中", color: neutral)
        case "checking": StateMeta(label: "校验中", color: Theme.info, pulse: true)
        case "completed": StateMeta(label: "等待入库", color: Theme.success)
        case "error": StateMeta(label: "下载异常", color: Theme.danger)
        case "missing": StateMeta(label: "任务缺失", color: Theme.danger)
        default: StateMeta(label: "状态未知", color: neutral)
        }
    }

    static func ingestStateMeta(_ status: String) -> StateMeta {
        switch status {
        case "queued": StateMeta(label: "等待入库", color: neutral)
        case "running": StateMeta(label: "正在入库", color: Theme.success, pulse: true)
        case "retry_wait": StateMeta(label: "等待重试", color: Theme.warning)
        case "cancelling": StateMeta(label: "正在停止", color: Theme.warning)
        case "waiting": StateMeta(label: "等待条件", color: neutral)
        case "blocked", "failed": StateMeta(label: "入库待处理", color: Theme.danger)
        case "succeeded": StateMeta(label: "入库完成", color: Theme.success)
        default: StateMeta(label: "入库已取消", color: neutral)
        }
    }

    /// Job 状态点颜色（job-center STATUS_STYLE）
    static func jobStatusMeta(_ status: String) -> StateMeta {
        let label = jobStatusLabels[status] ?? status
        switch status {
        case "running": return StateMeta(label: label, color: Theme.info, pulse: true)
        case "retry_wait", "cancelling": return StateMeta(label: label, color: Theme.warning)
        case "blocked", "failed": return StateMeta(label: label, color: Theme.danger)
        case "succeeded": return StateMeta(label: label, color: Theme.success)
        case "cancelled": return StateMeta(label: label, color: Color.white.opacity(0.3))
        default: return StateMeta(label: label, color: Color.white.opacity(0.4))
        }
    }
}

// MARK: - Job 文案推导

extension TaskCenter {
    static func jobString(_ job: API.JobView, _ key: String) -> String? {
        string(job.progress.details, key) ?? string(job.inputData, key)
    }

    static func jobTypeLabel(_ job: API.JobView) -> String { jobTypeLabels[job.jobType] ?? job.jobType }

    /// Job 的错误信息（error.message）
    static func errorMessage(_ job: API.JobView) -> String? { string(job.error, "message") }

    /// Job 错误里附带的补救动作
    struct ErrorAction: Hashable {
        var type: String
        var label: String
        var target: String?
    }

    static func errorActions(_ job: API.JobView) -> [ErrorAction] {
        guard case let .array(items)? = job.error?["actions"] else { return [] }
        return items.compactMap { item in
            guard let type = item["type"]?.stringValue, let label = item["label"]?.stringValue else { return nil }
            return ErrorAction(type: type, label: label, target: item["target"]?.stringValue)
        }
    }

    /// Feed 主标题优先使用执行结果确认过的作品名（《》/「」包裹）
    static func jobFeedIdentity(_ job: API.JobView) -> String? {
        let quoted = job.progress.message.firstMatch(of: /《([^》]+)》/).map { String($0.1) }
        guard let title = nonEmpty(quoted) ?? jobString(job, "media_title") ?? jobString(job, "title") ?? nonEmpty(job.subject) else {
            return nil
        }
        if title.wholeMatch(of: /^[《「].+[》」]$/) != nil { return title }
        if job.jobType == "library.scan" { return "「\(title)」" }
        if job.jobType == "library.ingest" || job.jobType == "subtitle.generate" || job.jobType.contains("metadata.refresh") {
            return "《\(title)》"
        }
        return title
    }

    /// 「现在」时间线的处理量（带「已处理」前缀）
    static func feedProgressAmount(_ job: API.JobView) -> String? {
        guard let current = job.progress.current, let total = job.progress.total else { return nil }
        if job.jobType == "library.ingest" {
            return "已处理 \(ActivityFormat.bytes(Double(current))) / \(ActivityFormat.bytes(Double(total)))"
        }
        let unit = job.jobType == "subtitle.generate" ? "个分块"
            : job.jobType == "library.scan" ? "个文件"
            : job.jobType.contains("metadata.refresh") ? "个条目" : "项"
        return "已处理 \(current) / \(total) \(unit)"
    }

    /// Job 卡片进度条下的处理量
    static func cardProgressAmount(_ job: API.JobView) -> String? {
        guard let current = job.progress.current, let total = job.progress.total else { return nil }
        if job.jobType == "library.ingest" {
            return "\(ActivityFormat.bytes(Double(current))) / \(ActivityFormat.bytes(Double(total)))"
        }
        let unit: String = switch job.jobType {
        case "subtitle.generate": "个分块"
        case "library.scan", "library.organize": "个文件"
        case "library.transfer": "个路径"
        default: job.jobType.contains("metadata.refresh") ? "个条目" : "项"
        }
        return "\(current) / \(total) \(unit)"
    }

    /// 「现在」时间线的发起方：「CLI 发起 · yee」
    static func feedOriginLabel(_ job: API.JobView) -> String {
        let origin = switch job.origin {
        case "cli": "CLI 发起"
        case "agent": "Agent 发起"
        case "scheduler", "system": "系统自动"
        default: "网页发起"
        }
        return job.actorName.map { "\(origin) · \($0)" } ?? origin
    }

    /// Job 卡片页脚的发起方：「CLI · yee」
    static func cardOriginLabel(_ job: API.JobView) -> String {
        let origin = switch job.origin {
        case "cli": "CLI"
        case "agent": "智能体"
        case "scheduler", "system": "系统自动"
        default: "网页"
        }
        return job.actorName.map { "\(origin) · \($0)" } ?? origin
    }

    static func activeJobStatus(_ job: API.JobView) -> String {
        if job.status == "running" { return activeJobActions[job.jobType] ?? "正在处理" }
        return jobStatusLabels[job.status] ?? job.status
    }

    /// 历史标题：「《某剧》入库完成」「「电影」扫描已取消」；忽略的失败如实写「未完成」
    static func historicalJobTitle(_ job: API.JobView) -> String {
        let identity = jobFeedIdentity(job)
        let result = job.status == "cancelled" ? "已取消" : job.status == "failed" ? "未完成" : "完成"
        if let identity, let action = completedJobActions[job.jobType] { return "\(identity)\(action)\(result)" }
        return "\(identity ?? jobTypeLabel(job))\(result)"
    }

    static func historicalJobSummary(_ job: API.JobView) -> String {
        if job.jobType == "library.scan" {
            var parts: [String] = []
            if let identified = number(job.progress.details, "identified") { parts.append("识别 \(Int(identified)) 个") }
            if let unidentified = number(job.progress.details, "unidentified") { parts.append("待识别 \(Int(unidentified)) 个") }
            if !parts.isEmpty { return parts.joined(separator: " · ") }
        }
        if job.status == "failed", isDismissed(job) {
            return errorMessage(job) ?? nonEmpty(job.progress.message) ?? "任务失败，已被忽略"
        }
        return nonEmpty(job.progress.message) ?? (job.status == "cancelled" ? "任务已取消" : "任务已完成")
    }

    /// 字幕任务的目标语言（「简体中文 + 英语（双语）」）
    static func subtitleLanguageLabel(_ job: API.JobView) -> String? {
        guard job.jobType == "subtitle.generate", let target = jobString(job, "target_language") else { return nil }
        let primary = languageLabels[target] ?? target
        guard let secondary = jobString(job, "secondary_language") else { return primary }
        return "\(primary) + \(languageLabels[secondary] ?? secondary)（双语）"
    }

    /// 卡片上的领域明细小标签（最多 4 个，alert 为琥珀色）
    struct DetailItem: Hashable {
        var label: String
        var alert = false
    }

    static func jobDetailItems(_ job: API.JobView) -> [DetailItem] {
        let details = job.progress.details
        var items: [DetailItem] = []
        func int(_ key: String) -> Int? { number(details, key).map { Int($0) } }
        switch job.jobType {
        case "subtitle.generate":
            if let language = subtitleLanguageLabel(job) { items.append(DetailItem(label: language)) }
            if let done = int("done_events"), let total = int("total_events"), total != 0 {
                items.append(DetailItem(label: "\(done) / \(total) 条字幕"))
            }
            if details["uses_ocr"]?.boolValue == true { items.append(DetailItem(label: "PGS OCR")) }
            if let parallelism = int("parallelism"), parallelism > 1 { items.append(DetailItem(label: "\(parallelism) 路并行")) }
            if let limits = int("rate_limit_count"), limits != 0 { items.append(DetailItem(label: "限流重试 \(limits) 次", alert: true)) }
        case "library.scan":
            if let v = int("identified"), v != 0 { items.append(DetailItem(label: "识别 \(v) 个")) }
            if let v = int("probed"), v != 0 { items.append(DetailItem(label: "补探 \(v) 个")) }
            if let v = int("unidentified"), v != 0 { items.append(DetailItem(label: "待识别 \(v) 个", alert: true)) }
            if let v = int("marked_missing"), v != 0 { items.append(DetailItem(label: "缺失 \(v) 个", alert: true)) }
            let errors = count(details, "errors")
            if errors != 0 { items.append(DetailItem(label: "\(errors) 个问题", alert: true)) }
        case "library.organize":
            let errors = count(details, "errors")
            if errors != 0 { items.append(DetailItem(label: "\(errors) 个问题", alert: true)) }
        case "library.transfer":
            if let moved = number(details, "bytes_moved"), moved != 0 {
                items.append(DetailItem(label: "已搬运 \(ActivityFormat.bytes(moved))"))
            } else if let total = number(details, "total_bytes"), total != 0 {
                items.append(DetailItem(label: "共 \(ActivityFormat.bytes(total))"))
            }
            if details["cross_device"]?.boolValue == true { items.append(DetailItem(label: "跨盘复制")) }
        case "library.ingest":
            if let name = string(details, "file_name") { items.append(DetailItem(label: name)) }
        default:
            if job.jobType.contains("metadata.refresh"), let failed = int("failed"), failed != 0 {
                items.append(DetailItem(label: "\(failed) 个未完成", alert: true))
            }
        }
        return Array(items.prefix(4))
    }

    /// 字幕任务的模型消耗
    struct UsageSummary {
        var requests: Int
        var failedRequests: Int
        var promptTokens: Int
        var completionTokens: Int
        var totalTokens: Int
        var cacheReadTokens: Int
        var totalDurationMs: Double
        var maxDurationMs: Double
    }

    static func subtitleUsageSummary(_ job: API.JobView) -> UsageSummary? {
        guard job.jobType == "subtitle.generate" else { return nil }
        func int(_ key: String) -> Int { Int(number(job.usage, key) ?? 0) }
        let summary = UsageSummary(
            requests: int("request_count"), failedRequests: int("failed_request_count"),
            promptTokens: int("prompt_tokens"), completionTokens: int("completion_tokens"),
            totalTokens: int("total_tokens"), cacheReadTokens: int("cache_read_tokens"),
            totalDurationMs: number(job.usage, "total_duration_ms") ?? 0,
            maxDurationMs: number(job.usage, "max_duration_ms") ?? 0
        )
        return summary.requests > 0 || summary.totalTokens > 0 || summary.totalDurationMs > 0 ? summary : nil
    }

    static func formatModelDuration(_ milliseconds: Double) -> String {
        if milliseconds <= 0 { return "—" }
        if milliseconds < 1000 { return "\(Int(milliseconds.rounded())) 毫秒" }
        return ActivityFormat.duration(seconds: max(1, (milliseconds / 1000).rounded()))
    }
}

// MARK: - 下载任务的状态推导

extension TaskCenter {
    /// 入库轴什么时候接管卡片主状态：入库受阻需要人处理，或整包已下完
    static func ingestOwnsTaskState(_ task: API.DownloadTaskView, ingestJob: API.JobView?) -> Bool {
        guard let ingestJob, ingestJob.status != "cancelled" else { return false }
        if jobNeedsAttention(ingestJob) { return true }
        return task.state == "completed"
    }

    static func isUpgradeTask(_ task: API.DownloadTaskView) -> Bool {
        task.subscriptions.first?.purpose == "upgrade"
    }

    /// 按逐集工单汇总交付进度：补缺数「已入库」、洗版数「已替换」
    struct UnitSummary {
        var done: Int
        var total: Int
        var upgrade: Bool
    }

    static func ingestUnitSummary(_ task: API.DownloadTaskView) -> UnitSummary? {
        guard let subscription = task.subscriptions.first, !subscription.units.isEmpty else { return nil }
        let upgrade = subscription.purpose == "upgrade"
        let done = subscription.units.filter { upgrade ? $0.replaced : $0.status == "imported" }.count
        return UnitSummary(done: done, total: subscription.units.count, upgrade: upgrade)
    }

    static func partialIngestLabel(_ task: API.DownloadTaskView) -> String? {
        guard let summary = ingestUnitSummary(task), summary.total > 1 else { return nil }
        return "\(summary.upgrade ? "已替换" : "已入库") \(summary.done)/\(summary.total) 集"
    }

    /// 时间线圆点：有在执行的（下载中/校验中/正在入库）为蓝，否则黄（等待）
    static func groupTimelineExecuting(_ group: DownloadGroup, ingestJobsByHash: [String: API.JobView]) -> Bool {
        group.tasks.contains { task in
            ingestJobsByHash[task.infoHash.lowercased()]?.status == "running" || ["downloading", "checking"].contains(task.state)
        }
    }

    static func sourceLabel(_ task: API.DownloadTaskView) -> String {
        switch task.source {
        case "subscription": "订阅投递"
        case "manual": "手动下载"
        default: "下载器任务"
        }
    }

    /// 下载任务的说明文字（Web `downloadTaskNote`）：原因优先，出路其次
    static func downloadTaskNote(_ task: API.DownloadTaskView, ingestJob: API.JobView?) -> String? {
        if let missing = contentMissingLabel(task) {
            return "种子里没有 \(missing)（声明的覆盖范围与实际内容不符），这部分已退回重新寻找资源；包里其余集不受影响"
        }
        if let ingestJob {
            switch ingestJob.status {
            case "succeeded":
                if let summary = ingestUnitSummary(task), summary.done < summary.total {
                    return summary.upgrade
                        ? "已完成 \(summary.done)/\(summary.total) 集替换，其余等待下载完成后自动校验替换"
                        : "已入库 \(summary.done)/\(summary.total) 集，其余等待下载完成后自动入库"
                }
                if ingestUnitSummary(task)?.upgrade == true {
                    return task.state != "completed" ? "已完成的部分已替换为新版本，其余等待下载完成" : "替换已完成，等待活动页同步收尾"
                }
                return task.state != "completed" ? "已完成的部分已入库，其余等待下载完成" : "入库已完成，等待活动页同步收尾"
            case "cancelled":
                return "上次入库已取消，等待重新处理"
            case "blocked", "failed":
                return errorMessage(ingestJob) ?? ingestJob.progress.message
            default:
                return ingestJob.progress.message
            }
        }
        if let landing = task.landingError { return landing }
        switch task.state {
        case "error":
            let reason = nonEmpty(task.errorMessage) ?? "下载器报告该任务出错，请在下载器中查看具体原因"
            if task.source == "external" { return "\(reason)（非 MovieClaw 投递的任务，仅观察）" }
            if let rescue = nonEmpty(task.rescueMessage) { return "\(reason)；\(rescue)" }
            return reason
        default: break
        }
        if let rescue = nonEmpty(task.rescueMessage) { return rescue }
        switch task.state {
        case "missing": return "已投递记录仍在，但下载器中找不到对应任务；系统确认后会自动退回重新寻找资源"
        case "completed": return "下载已完成，等待自动入库或媒体库扫描收尾"
        case "paused": return "已在下载器中暂停"
        case "queued": return "下载器正在排队，暂停无进度计时"
        case "checking": return "下载器正在校验数据，暂停无进度计时"
        case "stalled": return "暂无可用连接，等待继续下载"
        case "unknown": return "下载器未返回可识别的状态"
        default: return nil
        }
    }
}

// MARK: - 任务完整过程（DownloadLifecycle）

extension TaskCenter {
    enum StepTone { case done, current, waiting, attention, future }

    struct LifecycleStep: Hashable {
        var label: String
        var detail: String
        var tone: StepTone
    }

    /// 下载完成后还没发生的几步，按后端推导的落点如实展开
    static func plannedSteps(_ task: API.DownloadTaskView, downloaded: Bool, upgrade: Bool, keepOld: Bool) -> [LifecycleStep] {
        guard let plan = task.plan else {
            return [LifecycleStep(label: downloaded ? "等待入库" : "等待下载完成", detail: "下一步", tone: .future)]
        }
        if plan.mode == "downloader_default" {
            return [LifecycleStep(label: "不会自动入库", detail: "未配置媒体库或库缺少根路径，文件将留在下载器默认目录，需手动处理", tone: .attention)]
        }
        let libraryText = plan.libraryName.map { "「\($0)」" } ?? "按收藏范围自动匹配的媒体库"
        var steps: [LifecycleStep] = []
        if plan.mode == "watch" {
            let verb = plan.strategy == "hardlink" ? "硬链接" : plan.strategy == "copy" ? "复制" : "整理"
            steps.append(LifecycleStep(label: "待\(verb)", detail: plan.destPath.map { "\(verb)到 \($0)" } ?? "\(verb)到目标目录", tone: .future))
        }
        if !plan.entersLibrary {
            steps.append(LifecycleStep(label: "不直接入库", detail: "按规则整理到该目录后不写库台账，文件出现在库根时由扫描收尾", tone: .future))
            return steps
        }
        steps.append(LifecycleStep(
            label: "待入库",
            detail: plan.mode == "inplace" && plan.destPath != nil
                ? "已下载在库内目录，扫描后入库到\(libraryText)：\(plan.destPath!)"
                : "入库到\(libraryText)并刮削元数据",
            tone: .future
        ))
        if upgrade {
            steps.append(LifecycleStep(label: "待替换", detail: "校验档位，确认新版本更优才替换\(libraryText)中的旧版本", tone: .future))
            steps.append(keepOld
                ? LifecycleStep(label: "旧版本保留", detail: "按规则组的保留共存设置，旧版本不删除，与新版本并存", tone: .future)
                : LifecycleStep(label: "待回收", detail: "旧版本移入回收站，保留期内可恢复，到期自动清理释放空间", tone: .future))
        }
        return steps
    }

    /// 把下载器状态与关联入库 Job 投影成同一条业务过程（每一步都能回溯到事实源）
    static func lifecycleSteps(_ task: API.DownloadTaskView, ingestJob: API.JobView?) -> [LifecycleStep] {
        let downloaded = task.state == "completed"
        let downloadAttention = task.state == "error" || task.state == "missing"
        let downloadWaiting = ["stalled", "paused", "queued", "unknown"].contains(task.state)
        let percent = task.progress.map { Int(($0 * 100).rounded(.down)) }
        let source = nonEmpty(task.downloaderName) ?? "下载器"

        let downloadStep: LifecycleStep
        if downloadAttention {
            downloadStep = LifecycleStep(
                label: downloadStateMeta(task.state).label,
                detail: task.state == "missing" ? "等待恢复关联" : "需要在下载器中处理",
                tone: .attention
            )
        } else if downloaded {
            downloadStep = LifecycleStep(label: "下载完成", detail: task.sizeBytes.map { ActivityFormat.bytes(Double($0)) } ?? "文件已就绪", tone: .done)
        } else {
            downloadStep = LifecycleStep(
                label: downloadStateMeta(task.state).label,
                detail: percent.map { "\($0)%" } ?? "读取实时进度",
                tone: downloadWaiting ? .waiting : .current
            )
        }

        let partial = partialIngestLabel(task)
        let summary = ingestUnitSummary(task)
        let missing = contentMissingLabel(task)
        let fullyDelivered = summary.map { $0.done >= $0.total } ?? true
        let upgrade = summary?.upgrade == true

        var ingestStep: LifecycleStep?
        if let job = ingestJob, jobNeedsAttention(job) {
            ingestStep = LifecycleStep(label: "入库待处理", detail: errorMessage(job) ?? job.progress.message, tone: .attention)
        } else if let job = ingestJob, job.status == "failed", isDismissed(job) {
            ingestStep = LifecycleStep(label: "入库已忽略", detail: "已按你的选择不再提醒；如需处理可在「已结束」里撤销忽略", tone: .waiting)
        } else if let job = ingestJob, job.status == "running" {
            ingestStep = LifecycleStep(label: "正在入库", detail: job.progress.message, tone: .current)
        } else if let job = ingestJob, activeFeedJobStatuses.contains(job.status) {
            ingestStep = LifecycleStep(label: ingestStateMeta(job.status).label, detail: job.progress.message, tone: .waiting)
        } else if let missing {
            ingestStep = LifecycleStep(
                label: "内容不符",
                detail: (partial.map { "\($0)；" } ?? "") + "种子里没有 \(missing)，已退回重新寻找资源",
                tone: .attention
            )
        } else if (ingestJob?.status == "succeeded" && !upgrade) || (summary.map { $0.done > 0 } ?? false) {
            let done = downloaded && fullyDelivered
            let label = done ? (upgrade ? "替换完成" : "入库完成") : (upgrade ? "部分替换" : "部分入库")
            let detail = done
                ? (upgrade ? "已全部替换为新版本" : (ingestJob?.progress.message ?? "已全部入库"))
                : (upgrade ? "\(partial ?? "已替换")，其余等待下载与校验" : "\(partial ?? "已入库")，其余等待下载完成")
            ingestStep = LifecycleStep(label: label, detail: detail, tone: done ? .done : .waiting)
        } else if ingestJob?.status == "cancelled" {
            ingestStep = LifecycleStep(label: "入库已取消", detail: "等待重新处理", tone: .waiting)
        } else if task.landingError != nil {
            ingestStep = LifecycleStep(label: "无法入库", detail: "movieclaw 看不到已下载的文件", tone: .attention)
        } else if upgrade {
            ingestStep = LifecycleStep(
                label: downloaded ? "等待替换" : "等待下载完成",
                detail: downloaded ? "新版本入库后校验档位再替换" : "下一步",
                tone: .future
            )
        }
        let tail = ingestStep.map { [$0] } ?? plannedSteps(
            task, downloaded: downloaded, upgrade: upgrade,
            keepOld: task.subscriptions.first?.upgradeKeepOld == true
        )
        return [LifecycleStep(label: "已投递", detail: source, tone: .done), downloadStep] + tail
    }
}

// MARK: - 季集摘要（episode-units）

extension TaskCenter {
    struct EpisodeSummary {
        var isMovie: Bool
        var label: String
        var fullLabel: String
        var episodeCount: Int
        var seasons: [(season: Int, episodes: [Int])]
    }

    private struct EpisodeRange {
        var season: Int
        var start: Int
        var end: Int
    }

    private static func formatRanges(_ ranges: [EpisodeRange]) -> String {
        var seasons: [(season: Int, labels: [String])] = []
        for range in ranges {
            let label = range.start == range.end ? "E\(pad2(range.start))" : "E\(pad2(range.start))–E\(pad2(range.end))"
            if seasons.last?.season == range.season {
                seasons[seasons.count - 1].labels.append(label)
            } else {
                seasons.append((range.season, [label]))
            }
        }
        return seasons.map { "S\(pad2($0.season))\($0.labels.joined(separator: "、"))" }.joined(separator: " · ")
    }

    /// 连续集号压成区间；超过 3 个区间时只保留前 2 个并标出隐藏集数
    static func summarizeEpisodeUnits(_ units: [API.DownloadTaskUnitView]) -> EpisodeSummary? {
        guard !units.isEmpty else { return nil }
        if units.contains(where: { $0.seasonNumber == 0 && $0.episodeNumber == 0 }) {
            return EpisodeSummary(isMovie: true, label: "正片", fullLabel: "正片", episodeCount: 0, seasons: [])
        }
        var unique = Set<[Int]>()
        let sorted = units.filter { unique.insert([$0.seasonNumber, $0.episodeNumber]).inserted }
            .sorted { ($0.seasonNumber, $0.episodeNumber) < ($1.seasonNumber, $1.episodeNumber) }
        var seasons: [(season: Int, episodes: [Int])] = []
        for unit in sorted {
            if seasons.last?.season == unit.seasonNumber {
                seasons[seasons.count - 1].episodes.append(unit.episodeNumber)
            } else {
                seasons.append((unit.seasonNumber, [unit.episodeNumber]))
            }
        }
        var ranges: [EpisodeRange] = []
        for season in seasons {
            for episode in season.episodes {
                if let last = ranges.last, last.season == season.season, episode == last.end + 1 {
                    ranges[ranges.count - 1].end = episode
                } else {
                    ranges.append(EpisodeRange(season: season.season, start: episode, end: episode))
                }
            }
        }
        let full = formatRanges(ranges)
        if ranges.count <= 3 {
            return EpisodeSummary(isMovie: false, label: full, fullLabel: full, episodeCount: sorted.count, seasons: seasons)
        }
        let hidden = ranges.dropFirst(2).reduce(0) { $0 + $1.end - $1.start + 1 }
        return EpisodeSummary(
            isMovie: false, label: "\(formatRanges(Array(ranges.prefix(2)))) · 另 \(hidden) 集",
            fullLabel: full, episodeCount: sorted.count, seasons: seasons
        )
    }
}

// MARK: - 入库历史明细（ingest-history）

extension TaskCenter {
    struct IngestHistoryDetail {
        var summary: String
        var context: String?
        var fileGroups: [(label: String, files: [String])]
    }

    private static func readyFilePaths(_ value: API.JSONValue?) -> [String] {
        guard case let .array(items)? = value else { return [] }
        var seen = Set<String>()
        return items.compactMap { item -> String? in
            let path: String? = switch item {
            case let .string(value): value
            case let .object(object): object["path"]?.stringValue
            default: nil
            }
            guard let path, !path.isEmpty, seen.insert(path).inserted else { return nil }
            return path
        }
    }

    /// 从文件名提取 SxxEyy 并按季压缩连续集号
    private static func episodeLabel(_ files: [String]) -> String? {
        var seasons: [Int: Set<Int>] = [:]
        for file in files {
            for match in file.matches(of: /(?i)S(\d{1,3})[\s._-]*E(\d{1,4})/) {
                if let season = Int(match.1), let episode = Int(match.2) { seasons[season, default: []].insert(episode) }
            }
        }
        guard !seasons.isEmpty else { return nil }
        return seasons.keys.sorted().map { season in
            var ranges: [(Int, Int)] = []
            for episode in seasons[season]!.sorted() {
                if let last = ranges.last, episode == last.1 + 1 { ranges[ranges.count - 1].1 = episode } else { ranges.append((episode, episode)) }
            }
            let text = ranges.map { $0.0 == $0.1 ? "E\(pad2($0.0))" : "E\(pad2($0.0))–E\(pad2($0.1))" }.joined(separator: "、")
            return "S\(pad2(season))\(text)"
        }.joined(separator: " · ")
    }

    /// 历史入库行只陈述本次作业可以证实的结果
    static func ingestHistoryDetail(_ job: API.JobView) -> IngestHistoryDetail? {
        guard job.jobType == "library.ingest" else { return nil }
        let message = job.progress.message
        let imported = strings(job.progress.details, "imported_files")
        let present = strings(job.progress.details, "already_present_files")
        let ready = readyFilePaths(job.inputData["ready_files"])
        let noMove = message.contains(/已全部在库|无需搬运|未复制/)
        let context = message.firstMatch(of: /入库到(?:订阅指定的)?[「“"]([^」”"]+)[」”"]/).map { "已入库到「\($0.1)」" }

        if !imported.isEmpty {
            let summary = episodeLabel(imported).map { "新增 \($0) · 复制 \(imported.count) 个文件" } ?? "新增 \(imported.count) 个文件"
            var groups: [(String, [String])] = [("本次新增", imported)]
            if !present.isEmpty { groups.append(("已在库", present)) }
            return IngestHistoryDetail(summary: summary, context: context, fileGroups: groups)
        }
        if noMove {
            let existing = present.isEmpty ? ready : present
            let inspected = episodeLabel(existing).map { "检查 \($0)" } ?? (existing.isEmpty ? "检查完成" : "检查 \(existing.count) 个文件")
            return IngestHistoryDetail(summary: "\(inspected) · 均已在库，未复制", context: context, fileGroups: existing.isEmpty ? [] : [("已在库", existing)])
        }
        if let match = message.firstMatch(of: /复制\s*(\d+)\s*个文件/), let copied = Int(match.1), !ready.isEmpty {
            let inspected = episodeLabel(ready).map { "检查 \($0)" } ?? "检查 \(ready.count) 个文件"
            return IngestHistoryDetail(summary: "\(inspected) · 复制 \(copied) 个文件（旧记录未保存具体文件名）", context: context, fileGroups: [("本次检查", ready)])
        }
        return IngestHistoryDetail(
            summary: nonEmpty(message) ?? (job.status == "cancelled" ? "任务已取消" : "任务已完成"),
            context: context, fileGroups: present.isEmpty ? [] : [("已在库", present)]
        )
    }
}

// MARK: - 格式化（与 Web lib/format.ts、lib/time.ts 同口径）

/// 活动模块的格式化工具。字节格式与 Web `formatBytes` 逐字一致（「15.87 GB」「0 B」），
/// 不复用 `Formatters.bytes`（系统格式器的小数位与 Web 不同，同屏对照会对不上）。
enum ActivityFormat {
    static func bytes(_ value: Double?) -> String {
        guard let value, value.isFinite, value >= 0 else { return "—" }
        let units = ["B", "KB", "MB", "GB", "TB", "PB"]
        var number = value
        var index = 0
        while number >= 1024, index < units.count - 1 {
            number /= 1024
            index += 1
        }
        let rounded = (number * 100).rounded() / 100
        let digits = rounded >= 100 || index == 0 ? 0 : 2
        return String(format: "%.\(digits)f %@", rounded, units[index])
    }

    static func rate(_ bytesPerSecond: Double) -> String { "\(bytes(bytesPerSecond))/s" }

    /// 秒 → 「45 秒」「15 分钟」「1.5 小时」（Web `formatDuration`）
    static func duration(seconds: Double) -> String {
        guard seconds.isFinite, seconds > 0 else { return "—" }
        if seconds < 60 { return "\(Int(seconds)) 秒" }
        if seconds < 3600 { return "\(Int((seconds / 60).rounded())) 分钟" }
        let hours = seconds / 3600
        return hours == hours.rounded() ? "\(Int(hours)) 小时" : String(format: "%.1f 小时", hours)
    }

    /// 分钟 → 「2 小时 6 分钟」（Web `formatRuntimeMinutes`）
    static func runtime(minutes: Double) -> String {
        guard minutes.isFinite, minutes > 0 else { return "—" }
        let rounded = Int(minutes.rounded())
        if rounded < 60 { return "\(rounded) 分钟" }
        let remainder = rounded % 60
        return remainder > 0 ? "\(rounded / 60) 小时 \(remainder) 分钟" : "\(rounded / 60) 小时"
    }

    /// 毫秒观看时长 → 「2 小时 6 分钟」；不足一分钟按一分钟，零显示「—」
    static func watched(_ ms: Int) -> String {
        guard ms > 0 else { return "—" }
        return runtime(minutes: max(1, (Double(ms) / 60_000).rounded()))
    }

    /// 毫秒 → 播放器习惯的 h:mm:ss / m:ss
    static func playClock(ms: Int) -> String {
        let total = max(0, ms / 1000)
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }

    static func integer(_ value: Int) -> String { value.formatted(.number.locale(Locale(identifier: "zh_CN"))) }

    private static var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = Locale(identifier: "zh_CN")
        return calendar
    }

    /// 「18:05」（本地时区）
    static func clock(_ raw: String?) -> String {
        guard let date = Formatters.date(raw) else { return "—" }
        let c = calendar.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }

    /// 本地日期分组键「2026-09-25」
    static func dayKey(_ raw: String?) -> String {
        guard let date = Formatters.date(raw) else { return raw ?? "" }
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    /// 历史分组标题：今天早些时候 / 昨天 / 9月3日 / 2025年9月3日
    static func dayLabel(_ raw: String?) -> String {
        guard let date = Formatters.date(raw) else { return raw ?? "" }
        let calendar = calendar
        if calendar.isDateInToday(date) { return "今天早些时候" }
        if calendar.isDateInYesterday(date) { return "昨天" }
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        if c.year == calendar.component(.year, from: .now) { return "\(c.month ?? 0)月\(c.day ?? 0)日" }
        return "\(c.year ?? 0)年\(c.month ?? 0)月\(c.day ?? 0)日"
    }

    /// 相对时间「几秒前」「3 分钟前」（Web formatRelativeTime，算法统一在 Formatters.fromNow）
    static func relative(_ raw: String?) -> String {
        guard let date = Formatters.date(raw) else { return raw == nil ? "从未" : "" }
        return Formatters.fromNow(date)
    }

    /// 绝对时间「2026/09/25 18:05」
    static func dateTime(_ raw: String?) -> String {
        guard let date = Formatters.date(raw) else { return "—" }
        let c = calendar.dateComponents([.year, .month, .day, .hour, .minute], from: date)
        return String(format: "%04d/%02d/%02d %02d:%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0, c.hour ?? 0, c.minute ?? 0)
    }
}
