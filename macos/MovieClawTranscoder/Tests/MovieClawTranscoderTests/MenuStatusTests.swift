import AppKit
import XCTest

@testable import MovieClawTranscoder

/// 菜单状态卡片：内容换算与「长内容只长高、不长宽」。
///
/// 这一版改造的起因是用户反馈：菜单项不会折行，片名或错误一长，整个菜单就被
/// 撑成半屏宽。卡片固定宽度、文字折行，这里把它钉住。
@MainActor
final class MenuStatusTests: XCTestCase {
    private func status(
        _ state: WorkerConnectionState,
        name: String? = nil,
        progressMS: Int64? = nil,
        speed: String? = nil,
        error: String? = nil,
        message: String = "",
        activeJobs: Int = 0
    ) -> WorkerStatus {
        WorkerStatus(
            state: state,
            message: message,
            workerID: "Jerry-Mac-mini",
            activeJobs: activeJobs,
            maxJobs: 2,
            currentJobID: name == nil ? nil : "01M38T32D11MH695BA0PFKDWZK",
            currentJobName: name,
            currentProgress: progressMS.map { JobProgress(outTimeMS: $0, speed: speed, phase: "continue") },
            ffmpegVersion: "ffmpeg version 7.1.4-Jellyfin Copyright (c) 2000-2026 the FFmpeg developers",
            encoders: [],
            lastError: error,
            updatedAt: Date()
        )
    }

    func testBusyCardKeepsFullNameAndShowsProgress() {
        let name = "三国的星空第一部 (2025) - 2160p.WEB-DL.H265.HDR.DDP5.1-ADWeb.mkv"
        let model = MenuBarController.statusModel(
            status: status(.busy, name: name, progressMS: 5_025_000, speed: "5.21x", activeJobs: 2),
            configured: true,
            nasAddress: "http://192.168.1.10:3000/"
        )

        XCTAssertEqual(model.presentation.title, "转码中")
        XCTAssertEqual(model.subtitle, "Jerry-Mac-mini · 192.168.1.10:3000")
        // 片名不在数据层截断：折行与省略号交给视图，悬停提示里是完整的
        XCTAssertEqual(model.card?.title, name)
        XCTAssertEqual(model.card?.tooltip, name)
        XCTAssertEqual(model.card?.detail, "已转到 1:23:45 · 速度 5.2× · 另有 1 个任务")
        // 只留版本号：整行首句正是旧菜单被撑宽的原因之一
        XCTAssertEqual(model.footnote, "ffmpeg 7.1.4-Jellyfin · 并发 2/2")
    }

    func testReconnectingDoesNotRepeatTheErrorInTheCard() {
        let error = "NAS 控制连接断开：The Internet connection appears to be offline."
        let firstBeat = MenuBarController.statusModel(
            status: status(.reconnecting, error: error, message: error),
            configured: true,
            nasAddress: nil
        )
        XCTAssertEqual(firstBeat.card?.title, "正在重连 NAS")
        XCTAssertNil(firstBeat.card?.detail)
        XCTAssertEqual(firstBeat.error, error)

        let countdown = MenuBarController.statusModel(
            status: status(.reconnecting, error: error, message: "4 秒后重连"),
            configured: true,
            nasAddress: nil
        )
        XCTAssertEqual(countdown.card?.detail, "4 秒后重连")
    }

    func testUnpairedAndDisconnectedStatesTellTheUserWhatToDo() {
        let unpaired = MenuBarController.statusModel(status: nil, configured: false, nasAddress: nil)
        XCTAssertEqual(unpaired.presentation.title, "未配对")
        XCTAssertEqual(unpaired.card?.title, "还没有配对")

        let disconnected = MenuBarController.statusModel(
            status: nil, configured: true, nasAddress: "https://nas.example.com"
        )
        XCTAssertEqual(disconnected.presentation.title, "未连接")
        XCTAssertEqual(disconnected.subtitle, "已配对 · nas.example.com")
    }

    func testLongContentGrowsTallNotWide() {
        let view = MenuStatusView()
        view.apply(MenuBarController.statusModel(
            status: status(.ready), configured: true, nasAddress: "http://192.168.1.10:3000"
        ))
        let compact = view.frame.height

        let longName = String(repeating: "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.", count: 6) + "mkv"
        let longError = String(repeating: "产物上传失败：The network connection was lost. ", count: 8)
        view.apply(MenuBarController.statusModel(
            status: status(.busy, name: longName, progressMS: 812_000, speed: "3.4x", error: longError, activeJobs: 1),
            configured: true,
            nasAddress: "http://192.168.1.10:3000"
        ))

        XCTAssertEqual(view.frame.width, MenuStatusView.width, "菜单宽度必须固定，长文字只能折行")
        XCTAssertGreaterThan(view.frame.height, compact, "片名与错误卡片要把高度撑开")
        // 片名最多两行、错误最多三行：再长也不会无限长高
        XCTAssertLessThan(view.frame.height, 260)
    }

    func testDisplayTextFormatting() {
        XCTAssertEqual(DisplayText.ffmpegVersion("ffmpeg version 8.1.2-Jellyfin Copyright"), "8.1.2-Jellyfin")
        XCTAssertEqual(DisplayText.ffmpegVersion("检查中"), "检查中")
        XCTAssertEqual(DisplayText.clock(milliseconds: 5_025_000), "1:23:45")
        XCTAssertEqual(DisplayText.clock(milliseconds: 812_000), "13:32")
        XCTAssertEqual(DisplayText.speed("5.21x"), "5.2×")
        XCTAssertNil(DisplayText.speed("N/A"))
        XCTAssertEqual(DisplayText.host(of: "HTTP://10.1.1.5:3000//"), "10.1.1.5:3000")
        XCTAssertNil(DisplayText.host(of: "  "))
    }
}
