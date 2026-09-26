import Foundation
import Testing
@testable import MovieClaw

/// 任务中心的纯逻辑：归类与计数口径、下载任务说明、任务完整过程、季集摘要、入库历史、通知跳转。
/// 口径逐条对照 Web `lib/task-activity.ts`、`lib/download-attention.ts`、`components/task-center-view.tsx`、
/// `lib/episode-units.ts`、`lib/ingest-history.ts`、`lib/api/notices.ts`。
struct ActivityLogicTests {
    private func nn(_ value: Any?) -> Any { value ?? NSNull() }

    private func decode<T: Decodable>(_ json: [String: Any]) -> T {
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode(T.self, from: data)
    }

    private func job(
        id: String = "j1", type: String = "library.scan", status: String = "succeeded", dismissed: Bool = false,
        message: String = "", subject: String? = nil, resources: [[String: String]] = [], details: [String: Any] = [:],
        cancelledBy: String? = nil, updated: String = "2026-09-25T10:00:00+00:00"
    ) -> API.JobView {
        decode([
            "id": id, "job_type": type, "subject": nn(subject), "definition_version": 1, "handler_revision": "x",
            "status": status, "input_data": [:], "result": NSNull(), "error": NSNull(), "usage": [:],
            "progress": ["mode": "determinate", "phase": "completed", "message": message, "details": details],
            "resources": resources.map { ["resource_type": $0["type"]!, "resource_id": $0["id"]!, "relation": "target"] },
            "origin": "system", "attempt": 1, "max_attempts": 2, "revision": 1,
            "cancel_requested_by": nn(cancelledBy), "dismissed_at": nn(dismissed ? "2026-09-25T10:00:00+00:00" : nil),
            "created_at": updated, "updated_at": updated,
        ])
    }

    private func task(
        hash: String = "ABC", state: String = "downloading", source: String = "subscription", mediaItemId: Int? = 7,
        canReplace: Bool = false, units: [(Int, Int, String)] = [], purpose: String = "download", plan: [String: Any]? = nil,
        progress: Double = 0.5
    ) -> API.DownloadTaskView {
        let subscriptions: [[String: Any]] = units.isEmpty ? [] : [[
            "id": 22, "media_item_id": mediaItemId ?? 0, "media_title": "深渊无间", "media_kind": "tv", "purpose": purpose,
            "upgrade_keep_old": false,
            "units": units.map { ["season_number": $0.0, "episode_number": $0.1, "status": $0.2, "replaced": false, "content_missing": false] },
        ]]
        return decode([
            "id": "t-\(hash)", "info_hash": hash, "name": "Abyss.S01", "downloader_id": 1, "downloader_name": "qb",
            "progress": progress, "size_bytes": 17_040_000_000, "state": state, "source": source, "remux": false,
            "media_item_id": nn(mediaItemId), "media_title": "深渊无间", "media_kind": "tv",
            "plan": nn(plan), "subscriptions": subscriptions, "can_replace": canReplace,
        ])
    }

    @Test func attentionAndDismissed() {
        #expect(TaskCenter.jobNeedsAttention(job(status: "failed")))
        #expect(!TaskCenter.jobNeedsAttention(job(status: "failed", dismissed: true)))
        #expect(TaskCenter.jobIsHistorical(job(status: "failed", dismissed: true)))
        #expect(TaskCenter.isSystemCancelled(job(status: "cancelled", cancelledBy: "system:tree")))
    }

    @Test func activityCountsLinkIngestAndSkipBoost() {
        let ingest = job(id: "ingest", type: "library.ingest", status: "running", resources: [["type": "download", "id": "abc"]])
        let other = job(id: "scan", status: "failed")
        let history = job(id: "done", status: "succeeded")
        let downloads = [task(hash: "ABC"), task(hash: "B00", source: "boost", mediaItemId: nil)]
        let activity = TaskCenter.buildActivity(jobs: [ingest, other, history], downloads: downloads)
        // 入库 Job 串进了下载过程，不再单独计数；刷流不参与计数
        #expect(activity.activeTotal == 1)
        #expect(activity.attentionTotal == 1)
        #expect(activity.historyTotal == 1)
        #expect(activity.boostTasks.count == 1)
        #expect(activity.ingestJobsByHash["abc"]?.id == "ingest")
        #expect(TaskCenter.badge(activity) == TaskCenter.Badge(alert: true, count: 1, hint: "1 项需要处理"))
    }

    @Test func externalTasksNeverAlert() {
        #expect(!TaskCenter.downloadTaskNeedsAttention(task(state: "error", source: "external"), ingestJob: nil))
        #expect(TaskCenter.downloadTaskNeedsAttention(task(canReplace: true), ingestJob: nil))
    }

    @Test func notesAndLifecycle() {
        #expect(TaskCenter.downloadTaskNote(task(state: "stalled"), ingestJob: nil) == "暂无可用连接，等待继续下载")
        let plan: [String: Any] = ["mode": "watch", "strategy": "copy", "library_name": "剧集", "dest_path": "/m/剧集", "enters_library": true]
        let steps = TaskCenter.lifecycleSteps(task(state: "stalled", plan: plan, progress: 0), ingestJob: nil)
        #expect(steps.map(\.label) == ["已投递", "等待连接", "待复制", "待入库"])
        #expect(steps[2].detail == "复制到 /m/剧集")
        #expect(steps[3].detail == "入库到「剧集」并刮削元数据")
    }

    @Test func episodeSummary() {
        let units = (1 ... 16).map { (1, $0, "grabbed") }
        let summary = TaskCenter.summarizeEpisodeUnits(task(units: units).subscriptions[0].units)
        #expect(summary?.label == "S01E01–E16")
        let scattered = [(1, 1), (1, 3), (1, 5), (1, 7), (1, 8)].map { (u: (Int, Int)) in (u.0, u.1, "imported") }
        #expect(TaskCenter.summarizeEpisodeUnits(task(units: scattered).subscriptions[0].units)?.label == "S01E01、E03 · 另 3 集")
    }

    @Test func ingestHistory() {
        let j = job(type: "library.ingest", message: "已入库到「剧集」", details: ["imported_files": ["Show.S01E02.mkv", "Show.S01E03.mkv"]])
        let detail = TaskCenter.ingestHistoryDetail(j)
        #expect(detail?.summary == "新增 S01E02–E03 · 复制 2 个文件")
        #expect(detail?.context == "已入库到「剧集」")
        #expect(TaskCenter.historicalJobTitle(job(type: "library.ingest", message: "《深渊无间》入库完成")) == "《深渊无间》入库完成")
    }

    @Test func formats() {
        #expect(ActivityFormat.bytes(17_040_000_000) == "15.87 GB")
        #expect(ActivityFormat.bytes(0) == "0 B")
        #expect(ActivityFormat.watched(7_560_000) == "2 小时 6 分钟")
        #expect(ActivityFormat.duration(seconds: 5400) == "1.5 小时")
        #expect(ActivityFormat.playClock(ms: 630_000) == "10:30")
    }

    @Test func noticeRouting() {
        let child: API.NoticeView = decode([
            "id": 2, "severity": "error", "source": "ingest", "title": "t", "message": "m",
            "payload": ["grouped_under": "dir:/x"], "created_at": "", "updated_at": "",
        ])
        let parent: API.NoticeView = decode([
            "id": 1, "severity": "error", "source": "subscription", "title": "t", "message": "m",
            "payload": ["group_key": "dir:/x", "subscription_id": 9], "created_at": "", "updated_at": "",
        ])
        #expect(NoticeCenterView.visible([parent, child]).map(\.id) == [1])
        #expect(NoticeCenterView.visible([child]).map(\.id) == [2])
        #expect(NoticeCenterView.href(parent) == "/subscriptions/9")
        #expect(NoticeCenterView.href(child) == "/settings/import-watch")
    }
}
