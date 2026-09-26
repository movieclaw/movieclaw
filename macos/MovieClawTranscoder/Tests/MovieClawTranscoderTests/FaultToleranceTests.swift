import XCTest

@testable import MovieClawTranscoder

/// 容错规则（FaultTolerance.swift）与进程间消息（CoreIPC.swift）。
final class FaultToleranceTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_790_000_000)

    // MARK: 内核崩溃重启

    func testCrashRestartsBackOffThenGiveUpWithinTenMinutes() {
        var policy = CrashPolicy()
        XCTAssertEqual(policy.recordCrash(at: t0), .restart(after: 1, attempt: 1))
        XCTAssertEqual(policy.recordCrash(at: t0 + 10), .restart(after: 2, attempt: 2))
        XCTAssertEqual(policy.recordCrash(at: t0 + 20), .restart(after: 4, attempt: 3))
        XCTAssertEqual(policy.recordCrash(at: t0 + 30), .restart(after: 8, attempt: 4))
        XCTAssertEqual(policy.recordCrash(at: t0 + 40), .giveUp, "10 分钟内第 5 次崩溃就停手")
    }

    func testOldCrashesExpireAndDelayIsCapped() {
        var policy = CrashPolicy()
        for i in 0..<4 {
            _ = policy.recordCrash(at: t0 + TimeInterval(i))
        }
        // 11 分钟后再崩：前 4 次都过期了，从头算
        XCTAssertEqual(policy.recordCrash(at: t0 + 660), .restart(after: 1, attempt: 1))
        XCTAssertEqual(CrashPolicy.maxDelay, 60)
    }

    // MARK: 连续失败熔断

    func testThreeQuickFailuresTripTheBreaker() {
        var breaker = FailureBreaker()
        XCTAssertFalse(breaker.record(.failed, ranFor: 2, at: t0))
        XCTAssertFalse(breaker.record(.failed, ranFor: 3, at: t0 + 30))
        XCTAssertTrue(breaker.record(.failed, ranFor: 1, at: t0 + 60))
    }

    func testSuccessOrLongRunResetsButQuickSeekStopIsNeutral() {
        var breaker = FailureBreaker()
        _ = breaker.record(.failed, ranFor: 2, at: t0)
        _ = breaker.record(.failed, ranFor: 2, at: t0 + 10)
        // 拖进度条的秒停不说明任何问题：不清零也不累加
        XCTAssertFalse(breaker.record(.stopped, ranFor: 1, at: t0 + 20))
        XCTAssertEqual(breaker.quickFailures.count, 2)
        // 正常跑了一阵：ffmpeg 是好的，清零
        XCTAssertFalse(breaker.record(.stopped, ranFor: 300, at: t0 + 400))
        XCTAssertTrue(breaker.quickFailures.isEmpty)
        // 跑了很久才失败（网络断了、看门狗杀掉）不是这台 Mac 的毛病
        _ = breaker.record(.failed, ranFor: 2, at: t0 + 500)
        XCTAssertFalse(breaker.record(.failed, ranFor: 120, at: t0 + 600))
        XCTAssertTrue(breaker.quickFailures.isEmpty)
    }

    func testQuickFailuresSpreadBeyondWindowDoNotTrip() {
        var breaker = FailureBreaker()
        _ = breaker.record(.failed, ranFor: 2, at: t0)
        _ = breaker.record(.failed, ranFor: 2, at: t0 + 200)
        XCTAssertFalse(breaker.record(.failed, ranFor: 2, at: t0 + 400), "5 分钟窗口外的不算")
    }

    // MARK: 任务卡死看门狗

    func testWatchdogKillsSilentJobsButNotPausedOnes() {
        XCTAssertNil(JobWatchdog.verdict(startedAt: t0, lastProgressAt: t0 + 5, paused: false, now: t0 + 60))
        XCTAssertNotNil(JobWatchdog.verdict(startedAt: t0, lastProgressAt: t0 + 5, paused: false, now: t0 + 70))
        XCTAssertNil(JobWatchdog.verdict(startedAt: t0, lastProgressAt: t0 + 5, paused: true, now: t0 + 600),
                     "被 NAS 暂停的任务本来就没有进度")
        // 第一条进度多给些时间（连源、探测、初始化硬件编码器）
        XCTAssertNil(JobWatchdog.verdict(startedAt: t0, lastProgressAt: nil, paused: false, now: t0 + 80))
        let verdict = JobWatchdog.verdict(startedAt: t0, lastProgressAt: nil, paused: false, now: t0 + 95)
        XCTAssertEqual(verdict, "ffmpeg 起转 95 秒仍没有任何进度（卡住了），Worker 已强制结束它")
    }

    // MARK: NAS 拒绝理由

    func testRejectionReasonsFromServerAreClassified() {
        // 这几句是服务端 transcode_worker.py 里写死的原文
        XCTAssertEqual(NASRejection(reason: "凭证无效或已被吊销，请在网页「设置 → 设备」重新配对"), .authRejected)
        XCTAssertEqual(
            NASRejection(reason: "服务端尚未启用远程转码，请在网页「应用 → 远程转码」打开开关并确认地址"),
            .remoteDisabled
        )
        XCTAssertEqual(NASRejection(reason: "Worker 协议版本（1）与服务端（2）不一致，请把 Worker 与服务端更新到同一版本"), .other)
        XCTAssertEqual(NASRejectionError(reason: "凭证无效或已被吊销").errorDescription, "凭证无效或已被吊销")
    }

    func testHandshakeStatusCodesBecomeReadableReasons() {
        // 旧版服务端两种拒绝都只回一个 403：分不出是哪种，两种办法都得说，也不能按其中一种下结论
        let forbidden = WorkerClient.handshakeStatusError(403) as? NASRejectionError
        XCTAssertEqual(forbidden?.kind, .other)
        XCTAssertTrue(forbidden?.reason.contains("「应用 → 远程转码」") ?? false)
        XCTAssertTrue(forbidden?.reason.contains("重新配对") ?? false)
        XCTAssertEqual((WorkerClient.handshakeStatusError(404) as? NASRejectionError)?.kind, .other)
        // 5xx 是 NAS 暂时不可用（正在重启、更新），不算拒绝，照常退避
        let unavailable = WorkerClient.handshakeStatusError(502)
        XCTAssertNil(unavailable as? NASRejectionError)
        XCTAssertEqual(unavailable.localizedDescription, "NAS 暂时不可用（HTTP 502），稍后自动重连")
    }

    // MARK: 孤儿 ffmpeg

    func testOrphanDetectionRequiresAllThreeConditions() {
        let ffmpeg = "/tmp/ffmpeg-test"
        let orphan = OrphanSweeper.ProcessRecord(
            pid: 4321, parentPID: 1, executablePath: ffmpeg,
            arguments: ["ffmpeg", "-i", "http://10.0.0.2:3000/api/v1/transcode-worker/sessions/x/source",
                        "-progress", "pipe:1", "out.m3u8"]
        )
        XCTAssertTrue(OrphanSweeper.isOrphanedWorkerFFmpeg(orphan, ffmpegPath: ffmpeg))
        var alive = orphan
        alive.parentPID = 999
        XCTAssertFalse(OrphanSweeper.isOrphanedWorkerFFmpeg(alive, ffmpegPath: ffmpeg), "还有父进程管着，不是孤儿")
        var other = orphan
        other.executablePath = "/opt/homebrew/bin/ffmpeg"
        XCTAssertFalse(OrphanSweeper.isOrphanedWorkerFFmpeg(other, ffmpegPath: ffmpeg), "不是我们配置的 ffmpeg")
        var manual = orphan
        manual.arguments = ["ffmpeg", "-i", "movie.mkv", "out.mp4"]
        XCTAssertFalse(OrphanSweeper.isOrphanedWorkerFFmpeg(manual, ffmpegPath: ffmpeg), "用户自己跑的转码不能误杀")
    }

    // MARK: 进程间消息

    func testLinesAreSplitAndPartialLineIsKept() {
        var buffer = Data("{\"a\":1}\n\n{\"b\":2}\n{\"c\"".utf8)
        let lines = CoreLine.takeLines(from: &buffer)
        XCTAssertEqual(lines.map { String(decoding: $0, as: UTF8.self) }, ["{\"a\":1}", "{\"b\":2}"])
        XCTAssertEqual(String(decoding: buffer, as: UTF8.self), "{\"c\"", "半行留着等下次")
    }

    func testCommandsAndEventsRoundTrip() throws {
        let configuration = CoreConfiguration(WorkerConfiguration(
            nasURL: URL(string: "http://192.168.1.10:3000")!, workerToken: "secret-token",
            workerID: "macmini-m1", ffmpegPath: "/tmp/ffmpeg", maxJobs: 2
        ))
        for command in [CoreCommand.configure(configuration), .setDraining(true), .reconnectNow, .shutdown] {
            var line = try CoreLine.encode(command)
            XCTAssertEqual(line.last, 0x0A)
            line.removeLast()
            XCTAssertEqual(try CoreLine.decode(CoreCommand.self, from: line), command)
        }

        var status = WorkerStatus.offline(.draining, message: "暂停接单", workerID: "macmini-m1", maxJobs: 1,
                                          problem: .cooldown(until: t0, failures: 3))
        status = WorkerStatus(
            state: status.state, message: status.message, workerID: status.workerID, maxJobs: 1,
            jobs: [RunningJob(id: "j1", name: "抓特务.mkv", progress: JobProgress(outTimeMS: 1_000, speed: "3x", phase: "continue"),
                              startedAt: t0, videoEncoder: "h264_videotoolbox", paused: false)],
            ffmpegVersion: "8.1.2", encoders: ["h264_videotoolbox"], lastError: nil, updatedAt: t0,
            problem: .cooldown(until: t0, failures: 3)
        )
        var line = try CoreLine.encode(CoreEvent.status(status))
        line.removeLast()
        guard case let .status(decoded) = try CoreLine.decode(CoreEvent.self, from: line) else {
            return XCTFail("应解出状态")
        }
        XCTAssertEqual(decoded.jobs, status.jobs)
        XCTAssertEqual(decoded.problem, .cooldown(until: t0, failures: 3))
        XCTAssertEqual(decoded.state, .draining)
    }
}

/// 面板上的故障卡片与自动恢复说明。
@MainActor
final class ProblemNoticeTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_790_000_000)

    private func notice(_ problem: WorkerProblem, state: WorkerConnectionState = .error,
                        message: String = "说明") -> PanelModel.Notice? {
        let status = WorkerStatus.offline(state, message: message, workerID: "m", maxJobs: 1,
                                          error: message, problem: problem)
        let model = PanelModel.make(status: status, configured: true, nasAddress: nil, today: [], recent: [], now: now)
        XCTAssertEqual(model.badge, .attention)
        guard case let .notice(notice) = model.body else { return nil }
        return notice
    }

    func testEachProblemTellsWhatToDo() {
        XCTAssertEqual(notice(.authRejected)?.actions, [.pairAgain, .openLog])
        XCTAssertEqual(notice(.remoteDisabled, state: .reconnecting)?.title, "服务端还没打开远程转码")
        XCTAssertEqual(notice(.coreCrashLoop)?.actions, [.retry, .copyDiagnostics, .openLog])
        let recovering = notice(.coreRecovering(attempt: 2), state: .reconnecting, message: "转码内核刚才被信号 11 终止，2 秒后自动重启（第 2 次）")
        XCTAssertEqual(recovering?.spinning, true)
        XCTAssertTrue(recovering?.detail?.contains("菜单栏 App 不受影响") ?? false)
        XCTAssertEqual(notice(.ffmpegUnusable)?.actions, [.openSettings, .openLog])
        let cooldown = notice(.cooldown(until: now.addingTimeInterval(600), failures: 3), state: .draining)
        XCTAssertEqual(cooldown?.title, "暂停接单，正在自检")
        XCTAssertTrue(cooldown?.detail?.contains("连续 3 个任务刚开始就失败") ?? false)
    }

    func testRecoveryNoteSummarizesLast24Hours() {
        let status = WorkerStatus.offline(.ready, message: "", workerID: "m", maxJobs: 1)
        let recoveries = [
            CoreRecovery(at: now.addingTimeInterval(-90_000), reason: "太早了，不算"),
            CoreRecovery(at: now.addingTimeInterval(-3_600), reason: "35 秒无响应"),
            CoreRecovery(at: now.addingTimeInterval(-60), reason: "被信号 11 终止"),
        ]
        let model = PanelModel.make(status: status, configured: true, nasAddress: nil, today: [], recent: [],
                                    recoveries: recoveries, now: now)
        XCTAssertEqual(model.recoveryNote,
                       "24 小时内自动恢复过 2 次 · 最近 \(DisplayText.timeOfDay(now.addingTimeInterval(-60))) 被信号 11 终止")
        XCTAssertNil(PanelModel.make(status: status, configured: true, nasAddress: nil, today: [], recent: [],
                                     now: now).recoveryNote)
    }
}
