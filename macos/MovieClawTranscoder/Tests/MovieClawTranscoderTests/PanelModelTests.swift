import AppKit
import XCTest

@testable import MovieClawTranscoder

/// 状态面板：状态 → 面板内容的换算，以及「长内容只长高、不长宽」。
@MainActor
final class PanelModelTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_790_000_000)

    private func status(
        _ state: WorkerConnectionState,
        jobs: [RunningJob] = [],
        error: String? = nil,
        message: String = ""
    ) -> WorkerStatus {
        WorkerStatus(
            state: state,
            message: message,
            workerID: "Jerry-Mac-mini",
            maxJobs: 2,
            jobs: jobs,
            ffmpegVersion: "ffmpeg version 7.1.4-Jellyfin Copyright (c) 2000-2026 the FFmpeg developers",
            encoders: ["libx264", "h264_videotoolbox", "hevc_videotoolbox"],
            lastError: error,
            updatedAt: now
        )
    }

    private func job(
        _ name: String?,
        progressMS: Int64? = nil,
        speed: String? = nil,
        paused: Bool = false,
        id: String = "01M38T32D11MH695BA0PFKDWZK"
    ) -> RunningJob {
        RunningJob(
            id: id,
            name: name,
            progress: progressMS.map { JobProgress(outTimeMS: $0, speed: speed, phase: "continue") },
            startedAt: now,
            videoEncoder: "h264_videotoolbox",
            paused: paused
        )
    }

    private func model(_ status: WorkerStatus?, configured: Bool = true, today: [JobRecord] = [],
                       recent: [JobRecord] = [], speeds: [String: Double] = [:]) -> PanelModel {
        PanelModel.make(
            status: status, configured: configured, nasAddress: "http://192.168.1.10:3000/",
            today: today, recent: recent, speeds: speeds, now: now
        )
    }

    func testBusyCardsSayWhetherPlaybackIsSmooth() {
        let name = "三国的星空第一部 (2025) - 2160p.WEB-DL.H265.HDR.DDP5.1-ADWeb.mkv"
        let panel = model(
            status(.busy, jobs: [
                job(name, progressMS: 5_025_000, speed: "1.2x"),
                job(nil, id: "01M38T32D11MH695BA0PFKXYZW"),
            ]),
            speeds: ["01M38T32D11MH695BA0PFKDWZK": 3.44]
        )

        XCTAssertEqual(panel.presentation.title, "转码中")
        XCTAssertEqual(panel.subtitle, "Jerry-Mac-mini · 192.168.1.10:3000")
        XCTAssertEqual(panel.badge, .busy)
        guard case let .jobs(cards) = panel.body else { return XCTFail("转码中应显示任务卡片") }
        XCTAssertEqual(cards.count, 2)
        XCTAssertEqual(cards[0].title, "三国的星空第一部 (2025) - 2160p.WEB-DL.H265.HDR.DDP5.1-ADWeb")
        XCTAssertEqual(cards[0].encoder, "用硬件转成 H.264")
        XCTAssertEqual(cards[0].progress, "已准备到 1:23:45")
        // 用的是实时速度（3.44），不是 ffmpeg 报的平均值（1.2，被暂停拉低过）
        XCTAssertEqual(cards[0].health, .smooth(speed: 3.44))
        XCTAssertEqual(cards[0].explanation, "转码比播放快 3.4 倍，正在提前准备后面的画面。")
        // 数字只进悬停提示
        XCTAssertEqual(cards[0].tooltip, "\(name)\n实时转码速度 3.4 倍（不含暂停）\n已准备到片中 1:23:45")
        // 旧版服务端不下发片名时退回 job id 前缀；还没有进度就是「正在准备画面」
        XCTAssertEqual(cards[1].title, "任务 01M38T32")
        XCTAssertEqual(cards[1].health, .starting)
        XCTAssertNil(cards[1].progress)
        XCTAssertEqual(panel.footnote, "ffmpeg 7.1.4 · 硬件编码 H.264 / HEVC")
    }

    func testRealPlaybackPositionDrivesTheCard() {
        var running = job("抓特务 (2026).mkv", progressMS: 1_700_000)
        running.playback = JobPlayback(positionMS: 1_510_000, viewerPaused: false,
                                       durationMS: 6_730_000, preparedMS: 1_620_000)
        guard case let .jobs(cards) = model(status(.busy, jobs: [running]),
                                            speeds: ["01M38T32D11MH695BA0PFKDWZK": 3.4]).body
        else { return XCTFail() }
        XCTAssertEqual(cards[0].progress, "看到 25:10 / 1:52:10")
        XCTAssertEqual(cards[0].watchedFraction ?? 0, 1_510_000.0 / 6_730_000, accuracy: 1e-9)
        // 准备到哪儿以 NAS 按分片算的为准（1_620_000），不是 ffmpeg 的输出位置
        XCTAssertEqual(cards[0].preparedFraction ?? 0, 1_620_000.0 / 6_730_000, accuracy: 1e-9)
        XCTAssertEqual(cards[0].explanation, "转码比播放快 3.4 倍，已经领先观众 1 分 50 秒。")

        running.playback = JobPlayback(positionMS: 1_510_000, viewerPaused: true,
                                       durationMS: 6_730_000, preparedMS: 1_620_000)
        guard case let .jobs(paused) = model(status(.busy, jobs: [running])).body else { return XCTFail() }
        XCTAssertEqual(paused[0].health, .viewerPaused)
        XCTAssertEqual(paused[0].explanation, "后面的画面已经准备到 27:00，继续播放不用等。")
    }

    func testPreparedPositionIsOnTheFilmTimelineAfterSeek() {
        // 观众拖到 1:12:00，NAS 用 -ss 4320 重启；ffmpeg 从 0 数，转出 45 秒
        var running = job("抓特务 (2026).mkv", progressMS: 45_000)
        running.startOffsetMS = 4_320_000
        guard case let .jobs(cards) = model(status(.busy, jobs: [running])).body else { return XCTFail() }
        XCTAssertEqual(cards[0].progress, "已准备到 1:12:45", "不能写成「已准备到 0:45」")
    }

    func testSeekOffsetComesFromInputSideSS() {
        XCTAssertEqual(WorkerClient.seekOffsetMS(in: ["-nostdin", "-ss", "4956.000", "-hwaccel", "videotoolbox", "-i", "x"]), 4_956_000)
        XCTAssertEqual(WorkerClient.seekOffsetMS(in: ["-i", "x", "-ss", "30", "out"]), 0, "输出侧的 -ss 不是起点")
        XCTAssertEqual(WorkerClient.seekOffsetMS(in: ["-i", "x"]), 0)
    }

    func testFFmpegOutTimeMsIsActuallyMicroseconds() {
        // ffmpeg -progress 的 out_time_ms 单位是微秒：73_383_333 = 73 秒，不是 20 小时
        XCTAssertEqual(JobProgress.outTimeMilliseconds(["out_time_ms": "73383333"]), 73_383)
        XCTAssertEqual(JobProgress.outTimeMilliseconds(["out_time_us": "1500000", "out_time_ms": "9"]), 1_500)
        XCTAssertEqual(JobProgress.outTimeMilliseconds(["out_time": "00:25:10.500000"]), 1_510_500)
        XCTAssertNil(JobProgress.outTimeMilliseconds(["out_time_ms": "N/A", "out_time": "N/A"]))
    }

    func testHealthThresholds() {
        XCTAssertEqual(PanelModel.Health(speed: 1.3), .smooth(speed: 1.3))
        XCTAssertEqual(PanelModel.Health(speed: 1.1), .tight(speed: 1.1))
        XCTAssertEqual(PanelModel.Health(speed: 0.8), .lagging(speed: 0.8))
        XCTAssertEqual(PanelModel.Health(speed: 0.8).tone, .critical)
        XCTAssertEqual(PanelModel.Health.times(3.0), "3 倍")
        XCTAssertEqual(PanelModel.Health.times(2.46), "2.5 倍")
        // 刚起转、还没有实时速度时，退回 ffmpeg 自己报的速度
        guard case let .jobs(cards) = model(status(.busy, jobs: [job("a.mkv", progressMS: 3_000, speed: "0.7x")])).body
        else { return XCTFail() }
        XCTAssertEqual(cards[0].health, .lagging(speed: 0.7))
    }

    func testPausedJobIsReportedAsAheadNotAsAProblem() {
        let panel = model(status(.paused, jobs: [job("仙逆 S01E12.mkv", progressMS: 60_000, speed: "3.0x", paused: true)]),
                          speeds: ["01M38T32D11MH695BA0PFKDWZK": 4])
        guard case let .jobs(cards) = panel.body else { return XCTFail() }
        XCTAssertEqual(cards[0].health, .ahead)
        XCTAssertEqual(cards[0].health.title, "已提前准备好")
        XCTAssertFalse(cards[0].tooltip?.contains("实时转码速度") ?? true, "暂停时不报速度")
    }

    func testDrainingBeforeFFmpegUpdateStillShowsTheJob() {
        // 排空只在更新 ffmpeg 前出现：手上的任务照常显示，转完才换
        let working = model(status(.draining, jobs: [job("a.mkv", progressMS: 1_000)]))
        XCTAssertEqual(working.presentation.title, "暂停接单")
        guard case .jobs = working.body else { return XCTFail() }
        XCTAssertEqual(working.badge, .busy)
        XCTAssertEqual(model(status(.draining)).badge, .none)
    }

    func testNASPauseStillReadsAsTranscodingInHeader() {
        let panel = model(status(.paused, jobs: [job("a.mkv", progressMS: 1_000, paused: true)]))
        XCTAssertEqual(panel.presentation.title, "转码中", "NAS 让任务歇着不是出问题，胶囊别写「暂停」")
    }

    func testIdleShowsTodaySummaryAndRecentJobs() {
        let records = [
            JobRecord(id: "a", name: "仙逆 S01E12.mkv", outcome: .finished, error: nil,
                      endedAt: now.addingTimeInterval(-120), mediaMS: 3_600_000, elapsed: 1_000),
            JobRecord(id: "b", name: "沙丘2.mkv", outcome: .stopped, error: nil,
                      endedAt: now.addingTimeInterval(-7_300), mediaMS: 1_800_000, elapsed: 750),
            JobRecord(id: "c", name: nil, outcome: .failed, error: "ffmpeg 退出码：1",
                      endedAt: now.addingTimeInterval(-10), mediaMS: 0, elapsed: 1),
        ]
        let panel = model(status(.ready), today: records, recent: records)
        guard case let .idle(summary, rows) = panel.body else { return XCTFail("空闲应显示统计") }
        XCTAssertEqual(summary, PanelModel.Summary(count: 3, media: "1.5 小时", failures: 1))
        XCTAssertEqual(rows.map(\.title), ["仙逆 S01E12", "沙丘2", "任务 c"])
        XCTAssertEqual(rows.map(\.detail), ["整部转完 · 2 分钟前", "转出 30 分钟的片子 · 2 小时前", "失败 · 刚刚"])
        XCTAssertEqual(rows[2].tooltip, "ffmpeg 退出码：1", "失败原因要能悬停看到")
    }

    func testIdleWithoutHistoryHasNoSummary() {
        guard case let .idle(summary, rows) = model(status(.ready)).body else { return XCTFail() }
        XCTAssertNil(summary)
        XCTAssertTrue(rows.isEmpty)
    }

    func testReconnectingShowsReasonAndCountdownWithoutRepeating() {
        let error = "NAS 控制连接断开：The Internet connection appears to be offline."
        let firstBeat = model(status(.reconnecting, error: error, message: error))
        guard case let .notice(notice) = firstBeat.body else { return XCTFail() }
        XCTAssertEqual(notice.title, "正在重连 NAS")
        XCTAssertEqual(notice.detail, error)
        XCTAssertEqual(notice.tone, .warning)
        XCTAssertEqual(notice.actions, [.reconnect, .copyDiagnostics])
        XCTAssertEqual(firstBeat.badge, .attention)

        guard case let .notice(countdown) = model(status(.reconnecting, error: error, message: "4 秒后重连")).body
        else { return XCTFail() }
        XCTAssertEqual(countdown.detail, "\(error)\n4 秒后重连")
    }

    func testUnpairedAndDisconnectedStatesTellTheUserWhatToDo() {
        let unpaired = model(nil, configured: false)
        XCTAssertEqual(unpaired.presentation.title, "未配对")
        guard case let .notice(pair) = unpaired.body else { return XCTFail() }
        XCTAssertEqual(pair.title, "还没有配对")
        XCTAssertEqual(pair.actions, [.openSettings])

        let disconnected = model(nil)
        XCTAssertEqual(disconnected.presentation.title, "未连接")
        XCTAssertEqual(disconnected.subtitle, "已配对 · 192.168.1.10:3000")
        guard case let .notice(connect) = disconnected.body else { return XCTFail() }
        XCTAssertEqual(connect.actions, [.connect])
    }

    func testLongContentGrowsTallNotWide() {
        let view = PanelView()
        view.apply(model(status(.ready)))
        let compact = view.frame.height

        let longName = String(repeating: "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.", count: 6) + "mkv"
        view.apply(model(status(.busy, jobs: [job(longName, progressMS: 812_000, speed: "3.4x")])))
        XCTAssertEqual(view.frame.width, PanelView.width, "面板宽度必须固定，长文字只能折行")

        let longError = String(repeating: "产物上传失败：The network connection was lost. ", count: 12)
        view.apply(model(status(.error, error: longError)))
        XCTAssertEqual(view.frame.width, PanelView.width)
        XCTAssertGreaterThan(view.frame.height, compact / 2)
        // 错误最多四行：再长也不会无限长高
        XCTAssertLessThan(view.frame.height, 330)
    }

    func testDisplayTextFormatting() {
        XCTAssertEqual(DisplayText.ffmpegVersion("ffmpeg version 8.1.2-Jellyfin Copyright"), "8.1.2-Jellyfin")
        XCTAssertEqual(DisplayText.ffmpegVersion("检查中"), "检查中")
        XCTAssertEqual(DisplayText.clock(milliseconds: 5_025_000), "1:23:45")
        XCTAssertEqual(DisplayText.clock(milliseconds: 812_000), "13:32")
        XCTAssertEqual(DisplayText.speedValue("5.21x"), 5.21)
        XCTAssertNil(DisplayText.speedValue("N/A"))
        XCTAssertEqual(DisplayText.host(of: "HTTP://10.1.1.5:3000//"), "10.1.1.5:3000")
        XCTAssertNil(DisplayText.host(of: "  "))
        XCTAssertEqual(DisplayText.withoutExtension("Mr.Robot.S01E01.mkv"), "Mr.Robot.S01E01")
        XCTAssertEqual(DisplayText.withoutExtension("Mr.Robot"), "Mr.Robot")
        XCTAssertEqual(DisplayText.duration(milliseconds: 38 * 60_000), "38 分钟")
        XCTAssertEqual(DisplayText.duration(milliseconds: 252 * 60_000), "4.2 小时")
        XCTAssertEqual(DisplayText.duration(milliseconds: 120 * 60_000), "2 小时")
        XCTAssertEqual(DisplayText.encoder("hevc_videotoolbox"), "用硬件转成 HEVC")
        XCTAssertEqual(DisplayText.encoder("copy"), "原样转发，不重新编码")
        XCTAssertEqual(WorkerClient.videoEncoder(in: ["-i", "x", "-c:v", "h264_videotoolbox"]), "h264_videotoolbox")
        XCTAssertNil(WorkerClient.videoEncoder(in: ["-i", "x", "-c:v"]))
    }
}

/// 本地任务记录：seek 重启合并成一条、只留最近的、「今天」按本地日期算。
final class JobHistoryTests: XCTestCase {
    private func defaults() -> UserDefaults {
        let name = "JobHistoryTests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defaults.removePersistentDomain(forName: name)
        return defaults
    }

    private func record(_ id: String, at date: Date, outcome: JobRecord.Outcome = .stopped,
                        mediaMS: Int64 = 60_000, elapsed: TimeInterval = 20) -> JobRecord {
        JobRecord(id: id, name: "\(id).mkv", outcome: outcome, error: nil, endedAt: date, mediaMS: mediaMS, elapsed: elapsed)
    }

    func testSeekRestartsOfTheSameJobMergeIntoOneRecord() {
        let history = JobHistory(defaults: defaults())
        let start = Date()
        history.record(record("a", at: start))
        history.record(record("b", at: start.addingTimeInterval(5)))
        // 同一个 job id 拖了一下进度条又转完了：合并，结果取最新一轮，片长与耗时累加
        history.record(record("a", at: start.addingTimeInterval(60), outcome: .finished))

        let recent = history.recent(limit: 10)
        XCTAssertEqual(recent.map(\.id), ["a", "b"])
        XCTAssertEqual(recent[0].outcome, .finished)
        XCTAssertEqual(recent[0].mediaMS, 120_000)
        XCTAssertEqual(recent[0].elapsed, 40)
    }

    func testSameIDLongAfterwardsIsANewRecord() {
        let history = JobHistory(defaults: defaults())
        let start = Date()
        history.record(record("a", at: start))
        history.record(record("a", at: start.addingTimeInterval(3 * 3_600)))
        XCTAssertEqual(history.recent(limit: 10).count, 2)
    }

    func testPersistsAcrossInstancesAndFiltersToday() {
        let store = defaults()
        let now = Date()
        let history = JobHistory(defaults: store)
        history.record(record("old", at: now.addingTimeInterval(-2 * 24 * 3_600)))
        history.record(record("new", at: now))

        let reloaded = JobHistory(defaults: store)
        XCTAssertEqual(reloaded.recent(limit: 10).map(\.id), ["new", "old"])
        XCTAssertEqual(reloaded.today(now: now).map(\.id), ["new"])
    }

    func testLegacyRecordsWithWrongUnitsAreDropped() {
        let store = defaults()
        store.set(Data("[]".utf8), forKey: "movieclaw.jobHistory")
        _ = JobHistory(defaults: store)
        XCTAssertNil(store.object(forKey: "movieclaw.jobHistory"), "旧键里的片长大了 1000 倍，要丢掉")
    }
}

/// 实时速度：相邻两次进度之差 ÷ 时间差；暂停丢基线，暂停的时间不算。
final class SpeedTrackerTests: XCTestCase {
    func testPauseIsExcludedFromSpeed() {
        var tracker = SpeedTracker()
        let t0 = Date()
        tracker.observe(position: 0, at: t0)
        XCTAssertNil(tracker.speed, "一次采样算不出速度")
        tracker.observe(position: 12_000, at: t0.addingTimeInterval(4))
        XCTAssertEqual(tracker.speed ?? 0, 3, accuracy: 0.001)

        // NAS 暂停了 60 秒：基线作废，恢复后从新的一点重算
        tracker.base = nil
        tracker.observe(position: 12_000, at: t0.addingTimeInterval(64))
        tracker.observe(position: 24_000, at: t0.addingTimeInterval(68))
        XCTAssertEqual(tracker.speed ?? 0, 3, accuracy: 0.001, "暂停的 60 秒不能把速度拉低")
    }

    func testSamplesCloserThanTwoSecondsAreSkippedAndSeekBackIgnored() {
        var tracker = SpeedTracker()
        let t0 = Date()
        tracker.observe(position: 10_000, at: t0)
        tracker.observe(position: 11_000, at: t0.addingTimeInterval(0.5))
        XCTAssertNil(tracker.speed)
        tracker.observe(position: 2_000, at: t0.addingTimeInterval(3))
        XCTAssertNil(tracker.speed, "位置倒退（seek 重启）这一段不算")
    }
}

/// CPU / 内存小图表：数字怎么写、图按什么刻度画、没连上时不显示。
@MainActor
final class ResourceGaugeTests: XCTestCase {
    private func sample(_ cpu: Double, _ system: Double, memoryMB: UInt64) -> ResourceMonitor.Sample {
        ResourceMonitor.Sample(transcoderCPU: cpu, systemCPU: system,
                               transcoderMemory: memoryMB * 1_048_576,
                               systemMemory: 13_100_000_000)
    }

    func testGaugesDescribeTranscoderAgainstWholeMachine() {
        let history = PanelModel.ResourceHistory(
            samples: [sample(0.004, 0.2, memoryMB: 100), sample(0.184, 0.342, memoryMB: 420)],
            physicalMemory: 17_179_869_184, cores: 8
        )
        guard let resources = PanelModel.resources(history) else { return XCTFail() }
        XCTAssertEqual(resources.cpu.value, "18%")
        XCTAssertEqual(resources.cpu.detail, "整机 34%")
        XCTAssertEqual(resources.cpu.series, [0.004, 0.184])
        XCTAssertEqual(resources.cpu.background, [0.2, 0.342])
        XCTAssertTrue(resources.cpu.tooltip.contains("8 个核合计 100%"))
        XCTAssertEqual(resources.memory.value, "420 MB")
        XCTAssertEqual(resources.memory.detail, "整机 12.2 / 16 GB")
        // 内存图按转码自己的峰值（×1.25）定刻度，峰值那一格在 0.8
        XCTAssertEqual(resources.memory.series.last ?? 0, 0.8, accuracy: 1e-9)
    }

    func testIdleShowsZeroAndKeepsAppOverheadInTooltip() {
        // 没在转：ffmpeg 不存在，转码一栏就是 0；App 自己的开销只在悬停提示里
        var idle = sample(0, 0.12, memoryMB: 0)
        idle.appCPU = 0.002
        idle.appMemory = 29 * 1_048_576
        let history = PanelModel.ResourceHistory(samples: [idle], physicalMemory: 17_179_869_184, cores: 8)
        guard let resources = PanelModel.resources(history) else { return XCTFail() }
        XCTAssertEqual(resources.cpu.value, "0%")
        XCTAssertEqual(resources.memory.value, "0 MB")
        XCTAssertTrue(resources.cpu.tooltip.hasSuffix("本 App 自身（界面 + 转码内核）：<1%，不算在转码里。"))
        XCTAssertTrue(resources.memory.tooltip.hasSuffix("本 App 自身（界面 + 转码内核）：29 MB，不算在转码里。"))
    }

    func testTinyUsageIsNotShownAsZero() {
        let history = PanelModel.ResourceHistory(samples: [sample(0.003, 0.1, memoryMB: 2_000)],
                                                 physicalMemory: 17_179_869_184, cores: 8)
        XCTAssertEqual(PanelModel.resources(history)?.cpu.value, "<1%")
        XCTAssertEqual(PanelModel.resources(history)?.memory.value, "2.0 GB")
    }

    func testNoGaugesWithoutSamplesOrConnection() {
        XCTAssertNil(PanelModel.resources(.init(samples: [], physicalMemory: 1, cores: 1)))
        let unpaired = PanelModel.make(
            status: nil, configured: false, nasAddress: nil, today: [], recent: [],
            resources: .init(samples: [sample(0.1, 0.2, memoryMB: 10)], physicalMemory: 1, cores: 1)
        )
        XCTAssertNil(unpaired.resources, "没配对时不显示资源图表")
    }
}
