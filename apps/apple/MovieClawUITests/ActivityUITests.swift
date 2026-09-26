import XCTest

/// 活动 / 待处理事项模块的端到端验收（只含安全操作）。
///
/// 服务器是用户正在使用的真实 MovieClaw，因此硬规则：
/// - 每次点击前都经 `safeTap`：断言目标存在、`isHittable`、且不与底部标签栏重叠（被遮挡时 XCUITest
///   会按坐标点到下层控件——另一个模块真出过误建订阅的事故），否则用例直接失败；
/// - 不出现任何真实写操作的**确认**点击：结束播放 / 注销设备 / 删除种子任务 / 取消、重试任务 /
///   全部忽略 / 忽略通知 都只打开确认界面，然后点「取消」离开；
/// - 「交给 AI 分析」会真实创建会话、消耗模型额度，默认跳过，只有显式设置 MC_ALLOW_HANDOFF=1 才跑一次。
///
/// 环境变量（scripts/test.sh 以 TEST_RUNNER_ 前缀转交）：MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD。
final class ActivityUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["MC_TEST_SERVER"] ?? "http://localhost:3000" }
    private var username: String { env["MC_TEST_USERNAME"] ?? "admin" }
    private var password: String { env["MC_TEST_PASSWORD"] ?? "mclaw-dev-2026" }

    private lazy var backend = ActivityBackend(server: server, username: username, password: password)

    // MARK: 启动与工具

    @MainActor
    private func launch(route: String) -> XCUIApplication {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing", "-mcServer", server, "-mcUser", username, "-mcPass", password, "-mcRoute", route]
        app.launch()
        return app
    }

    @MainActor
    private func snapshot(_ name: String) {
        let screenshot = XCUIScreen.main.screenshot()
        // 交付对照用：设置 MC_SHOT_DIR 时同时落盘一份（模拟器上的测试进程可直接写宿主机路径）
        if let dir = env["MC_SHOT_DIR"] {
            try? screenshot.pngRepresentation.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    @MainActor
    private func element(_ app: XCUIApplication, _ identifier: String) -> XCUIElement {
        app.descendants(matching: .any)[identifier].firstMatch
    }

    /// 底部标签栏的遮挡区（浮动液态玻璃栏）
    @MainActor
    private func tabBarFrame(_ app: XCUIApplication) -> CGRect? {
        let bar = app.tabBars.firstMatch
        return bar.exists ? bar.frame : nil
    }

    /// 把目标滚到标签栏之上（向上轻扫，最多 8 次）
    @MainActor
    private func scrollClearOfTabBar(_ app: XCUIApplication, _ target: XCUIElement) {
        for _ in 0 ..< 8 {
            guard target.exists else { return }
            let bar = tabBarFrame(app)
            let covered = bar.map { target.frame.maxY > $0.minY - 4 } ?? false
            if target.isHittable, !covered { return }
            let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.7))
            start.press(forDuration: 0.05, thenDragTo: app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.45)))
        }
    }

    /// 唯一的点击入口：存在、可点、未被标签栏遮挡，才点
    @MainActor
    private func safeTap(_ app: XCUIApplication, _ target: XCUIElement, _ what: String, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(target.waitForExistence(timeout: 15), "找不到「\(what)」", file: file, line: line)
        XCTAssertTrue(target.isHittable, "「\(what)」不可点（被遮挡或在屏幕外）", file: file, line: line)
        if let bar = tabBarFrame(app), app.alerts.count == 0, app.sheets.count == 0 {
            XCTAssertFalse(bar.intersects(target.frame), "「\(what)」与底部标签栏重叠，拒绝按坐标点击", file: file, line: line)
        }
        target.tap()
    }

    // MARK: 观看

    /// 正在播放：与后端快照一致（有会话出一行、没有时摘要写「现在没有人在看」）；有会话时长按打开「结束播放」确认框后取消
    @MainActor
    func testNowPlayingMatchesBackend() {
        let app = launch(route: "/activity")
        let sessions = backend.activitySessions()
        if sessions.isEmpty {
            let summary = element(app, "activity-summary")
            XCTAssertTrue(summary.waitForExistence(timeout: 20))
            expectation(for: NSPredicate(format: "label CONTAINS '现在没有人在看'"), evaluatedWith: summary)
            waitForExpectations(timeout: 20)
            snapshot("正在播放-空态")
            return
        }
        let card = element(app, "session-card")
        XCTAssertTrue(card.waitForExistence(timeout: 20), "后端有 \(sessions.count) 个会话，应显示会话卡片")
        let title = sessions[0]["title"] as? String ?? ""
        XCTAssertTrue(app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", title)).firstMatch.waitForExistence(timeout: 10),
                      "会话卡片应显示片名「\(title)」")
        snapshot("正在播放")
        // 长按会话行 → 菜单「结束播放」→ 只看确认框，点「取消」
        scrollClearOfTabBar(app, card)
        card.press(forDuration: 1.2)
        safeTap(app, app.buttons["结束播放"].firstMatch, "结束播放（菜单项，仅打开确认框）")
        let alert = app.alerts.firstMatch
        XCTAssertTrue(alert.waitForExistence(timeout: 5), "应弹出结束播放的确认框")
        XCTAssertTrue(alert.label.contains("本次播放"))
        snapshot("结束播放确认框")
        safeTap(app, alert.buttons["取消"], "确认框的取消")
        XCTAssertFalse(alert.waitForExistence(timeout: 2))
        XCTAssertFalse(backend.activitySessions().isEmpty, "取消后会话应仍在")
    }

    /// 最近播放：首行与后端第一条一致；滚到底自动续载第二页
    @MainActor
    func testRecentPlaysPaginates() throws {
        let app = launch(route: "/activity?view=plays")
        let page = backend.history()
        let entries = page["entries"] as? [[String: Any]] ?? []
        try XCTSkipIf(entries.isEmpty, "服务器上还没有播放记录")
        XCTAssertTrue(element(app, "history-list").waitForExistence(timeout: 20))
        let firstTitle = (entries[0]["media"] as? [String: Any])?["title"] as? String ?? ""
        XCTAssertTrue(app.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH %@", firstTitle)).firstMatch.exists,
                      "首行应是「\(firstTitle)」")
        snapshot("最近播放")
        guard page["has_more"] as? Bool == true else { return }
        // 连续上滑直到出现第二页的内容（第二页第一条的时间行不可预测，改看「加载更多」按钮随续载后移 / 到底提示）
        for _ in 0 ..< 25 {
            if element(app, "history-end").exists { break }
            app.swipeUp(velocity: .fast)
        }
        let loadedMore = backend.historyCount(pages: 2) > 30
        if loadedMore {
            XCTAssertTrue(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'history-load-more' OR label CONTAINS '加载'")).count > 0
                          || element(app, "history-end").exists,
                          "滚到底后应出现续载按钮或「已经到最早的记录了」")
        }
        snapshot("最近播放-滚动后")
    }

    /// 观看统计：播放场次与后端一致；TOP 3、热力图都在；切换周期
    @MainActor
    func testWatchStats() {
        let app = launch(route: "/activity?view=stats")
        let stats = backend.stats(days: 30)
        let plays = (stats["current"] as? [String: Any])?["plays"] as? Int ?? -1
        let card = app.buttons["metric-播放场次"]
        XCTAssertTrue(card.waitForExistence(timeout: 25), "应显示指标卡")
        XCTAssertTrue(card.label.contains("\(plays)"), "播放场次应为后端的 \(plays)（实际：\(card.label)）")
        snapshot("观看统计-指标卡")
        if plays > 0 {
            XCTAssertTrue(element(app, "favorite-podium").exists)
            let heatmap = element(app, "watch-heatmap")
            var page = 0
            for _ in 0 ..< 12 where !heatmap.isHittable {
                app.swipeUp()
                page += 1
                snapshot("观看统计-滚动\(page)")
            }
            XCTAssertTrue(heatmap.exists, "应显示观看时段热力图")
            snapshot("观看统计-热力图")
            for _ in 0 ..< 12 { app.swipeDown(velocity: .fast) }
        }
        // 周期切到 7 天（右上角筛选菜单），播放场次随之变化为后端 7 天口径
        safeTap(app, app.buttons["activity-filter"], "筛选菜单")
        safeTap(app, app.buttons["最近 7 天"].firstMatch, "最近 7 天")
        let plays7 = (backend.stats(days: 7)["current"] as? [String: Any])?["plays"] as? Int ?? -1
        let predicate = NSPredicate(format: "label CONTAINS %@", "\(plays7)")
        expectation(for: predicate, evaluatedWith: app.buttons["metric-播放场次"])
        waitForExpectations(timeout: 15)
    }

    // MARK: 任务

    /// 任务：SSE 已连上（收到 ready 事件）、「最近完成 · 查看全部 N 个」与后端口径一致、已结束页可见
    @MainActor
    func testTaskCenterLiveAndSections() {
        let app = launch(route: "/activity")
        let summary = element(app, "activity-summary")
        XCTAssertTrue(summary.waitForExistence(timeout: 20))
        // accessibilityValue = live-<事件数>：SSE 在线且至少收到一个 ready/job 事件
        let live = NSPredicate(format: "value BEGINSWITH 'live-' AND NOT (value == 'live-0')")
        expectation(for: live, evaluatedWith: summary)
        waitForExpectations(timeout: 20)
        snapshot("活动-总览")

        let history = backend.historicalJobCount()
        let finished = app.buttons["activity-finished-all"]
        if history > 0 {
            XCTAssertTrue(finished.waitForExistence(timeout: 10), "有已结束任务时应出现「最近完成」")
            scrollClearOfTabBar(app, finished)
            XCTAssertTrue(finished.label.contains("\(history)"), "已结束计数应为 \(history)（实际：\(finished.label)）")
            safeTap(app, finished, "最近完成 · 查看全部")
            XCTAssertTrue(element(app, "history-section").waitForExistence(timeout: 10))
            snapshot("任务-已结束")
            app.navigationBars.buttons.firstMatch.tap()
        } else {
            XCTAssertFalse(finished.waitForExistence(timeout: 3), "没有已结束任务时不应出现「最近完成」")
        }
        let active = app.buttons["activity-active-all"]
        if active.waitForExistence(timeout: 3) {
            safeTap(app, active, "进行中 · 查看全部")
            XCTAssertTrue(element(app, "active-section").waitForExistence(timeout: 10) || element(app, "task-empty").exists)
            snapshot("任务-进行中")
        }
    }

    /// 删除种子任务：只打开确认弹层、检查「同时删除数据文件」开关与按钮文案，然后取消（绝不确认）
    @MainActor
    func testDeleteTorrentDialogOnlyOpens() throws {
        let hash = backend.firstMediaTaskHash()
        try XCTSkipIf(hash == nil, "没有可删除的非刷流种子任务")
        // 需要处理的种子在总览上（完整卡片），进行中的在「进行中」二级页
        let app = launch(route: "/activity")
        let menu = element(app, "download-actions-\(hash!)")
        if !menu.waitForExistence(timeout: 15) {
            safeTap(app, app.buttons["activity-active-all"], "进行中 · 查看全部")
        }
        XCTAssertTrue(menu.waitForExistence(timeout: 25))
        scrollClearOfTabBar(app, menu)
        safeTap(app, menu, "种子任务操作菜单")
        safeTap(app, app.buttons["删除任务"].firstMatch, "删除任务（菜单项，仅打开确认弹层）")
        let confirm = app.buttons["delete-task-confirm"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        XCTAssertTrue(confirm.label.contains("仅删除任务"), "默认只删任务不删文件")
        snapshot("删除种子任务确认")
        safeTap(app, app.buttons["取消"].firstMatch, "弹层的取消")
        XCTAssertFalse(confirm.waitForExistence(timeout: 3))
        XCTAssertTrue(backend.hasDownloadTask(hash!), "取消后种子任务应仍在")
    }

    /// 忽略 → 撤销忽略（允许的唯一 Job 写操作）：需要一个失败且未忽略的任务，没有就跳过
    @MainActor
    func testDismissThenUndismissFailedJob() throws {
        let jobId = backend.failedUndismissedJobId()
        try XCTSkipIf(jobId == nil, "服务器上没有失败且未忽略的任务，跳过忽略/撤销")
        let app = launch(route: "/activity")
        let menu = element(app, "job-actions-\(jobId!)")
        XCTAssertTrue(menu.waitForExistence(timeout: 25))
        scrollClearOfTabBar(app, menu)
        safeTap(app, menu, "任务操作菜单")
        safeTap(app, app.buttons["忽略这个任务"].firstMatch, "忽略这个任务")
        let toggle = app.switches["dismiss-mute-toggle"]
        if toggle.waitForExistence(timeout: 3), (toggle.value as? String) == "1" {
            // 不静音自动来源，保证撤销后完全恢复原状
            safeTap(app, toggle, "静音来源开关")
        }
        safeTap(app, app.buttons["dismiss-job-confirm"], "忽略")
        XCTAssertTrue(waitUntil { self.backend.jobDismissed(jobId!) == true }, "后端应记录已忽略")
        // 立刻撤销：「最近完成 · 查看全部」进已结束页
        let finished = app.buttons["activity-finished-all"]
        XCTAssertTrue(finished.waitForExistence(timeout: 15))
        scrollClearOfTabBar(app, finished)
        safeTap(app, finished, "最近完成 · 查看全部")
        let undo = app.buttons["undismiss-\(jobId!)"]
        XCTAssertTrue(undo.waitForExistence(timeout: 15))
        scrollClearOfTabBar(app, undo)
        safeTap(app, undo, "撤销忽略")
        XCTAssertTrue(waitUntil { self.backend.jobDismissed(jobId!) == false }, "撤销后应恢复为未忽略")
    }

    /// 交给 AI 分析：真实创建会话，默认跳过；MC_ALLOW_HANDOFF=1 时对第一张需要处理的下载卡跑一次
    @MainActor
    func testHandoffCreatesSession() throws {
        try XCTSkipUnless(env["MC_ALLOW_HANDOFF"] == "1", "会消耗模型额度，需显式 MC_ALLOW_HANDOFF=1")
        let hash = backend.attentionDownloadHash()
        try XCTSkipIf(hash == nil, "没有需要处理的下载任务")
        let before = backend.sessionCount()
        let app = launch(route: "/activity")
        let button = app.buttons["handoff-download-\(hash!)"]
        XCTAssertTrue(button.waitForExistence(timeout: 25))
        scrollClearOfTabBar(app, button)
        safeTap(app, button, "交给 AI 分析")
        XCTAssertTrue(waitUntil(timeout: 60) { self.backend.sessionCount() > before }, "应新建一个 AI 会话")
        snapshot("交给 AI 分析后")
    }

    private func waitUntil(timeout: TimeInterval = 20, _ condition: @escaping () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            Thread.sleep(forTimeInterval: 1)
        }
        return false
    }
}

/// 测试进程里的最小后端客户端（独立会话，只读为主）
private final class ActivityBackend {
    let base: String
    let session: URLSession

    init(server: String, username: String, password: String) {
        base = server.hasSuffix("/") ? String(server.dropLast()) : server
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieAcceptPolicy = .always
        session = URLSession(configuration: config)
        _ = request("POST", "/auth/login", body: ["username": username, "password": password, "remember": true])
    }

    @discardableResult
    func request(_ method: String, _ path: String, body: Any? = nil) -> Any? {
        var request = URLRequest(url: URL(string: base + "/api/v1" + path)!)
        request.httpMethod = method
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        let semaphore = DispatchSemaphore(value: 0)
        var result: Any?
        session.dataTask(with: request) { data, _, _ in
            if let data, let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { result = json["data"] }
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 30)
        return result
    }

    /// 会话（附带 media.title 摊平到 title）
    func activitySessions() -> [[String: Any]] {
        let data = request("GET", "/playback/activity?scope=\(scope())") as? [String: Any]
        return (data?["sessions"] as? [[String: Any]] ?? []).map { session in
            var copy = session
            copy["title"] = (session["media"] as? [String: Any])?["title"]
            return copy
        }
    }

    /// App 默认口径是「我的浏览范围」（测试模拟器里没切过）
    func scope() -> String { "visible" }

    func history() -> [String: Any] {
        request("GET", "/playback/history?limit=30&scope=visible") as? [String: Any] ?? [:]
    }

    func historyCount(pages: Int) -> Int {
        var total = 0
        var cursor: Int?
        for _ in 0 ..< pages {
            let path = "/playback/history?limit=30&scope=visible" + (cursor.map { "&before=\($0)" } ?? "")
            let page = request("GET", path) as? [String: Any] ?? [:]
            total += (page["entries"] as? [Any])?.count ?? 0
            cursor = page["next_cursor"] as? Int
            if cursor == nil { break }
        }
        return total
    }

    func stats(days: Int) -> [String: Any] {
        let offset = TimeZone.current.secondsFromGMT() / 60
        return request("GET", "/playback/stats/watch?days=\(days)&scope=visible&tz_offset=\(offset)") as? [String: Any] ?? [:]
    }

    private func jobs() -> [[String: Any]] {
        let recent = (request("GET", "/jobs?limit=50") as? [String: Any])?["items"] as? [[String: Any]] ?? []
        let active = (request("GET", "/jobs?active_only=true&limit=200") as? [String: Any])?["items"] as? [[String: Any]] ?? []
        var merged: [String: [String: Any]] = [:]
        for job in recent + active { merged[job["id"] as? String ?? ""] = job }
        return Array(merged.values)
    }

    private func downloads() -> [[String: Any]] {
        (request("GET", "/downloaders/tasks") as? [String: Any])?["items"] as? [[String: Any]] ?? []
    }

    /// 与 App（Web task-activity）同口径的「已结束」计数：终态 + 已忽略的失败，去掉已串进下载生命周期的入库 Job
    func historicalJobCount() -> Int {
        let hashes = Set(downloads().compactMap { ($0["info_hash"] as? String)?.lowercased() })
        return jobs().filter { job in
            let status = job["status"] as? String ?? ""
            let dismissed = !(job["dismissed_at"] is NSNull) && job["dismissed_at"] != nil
            guard status == "succeeded" || status == "cancelled" || (status == "failed" && dismissed) else { return false }
            if job["job_type"] as? String == "library.ingest" {
                let linked = (job["resources"] as? [[String: Any]] ?? []).contains {
                    $0["resource_type"] as? String == "download" && hashes.contains(($0["resource_id"] as? String ?? "").lowercased())
                }
                if linked { return false }
            }
            return true
        }.count
    }

    func firstMediaTaskHash() -> String? {
        downloads().first { $0["source"] as? String != "boost" && !($0["downloader_id"] is NSNull) }?["info_hash"] as? String
    }

    func attentionDownloadHash() -> String? {
        downloads().first { task in
            task["source"] as? String != "boost" && task["source"] as? String != "external"
                && (task["can_replace"] as? Bool == true || ["error", "missing"].contains(task["state"] as? String ?? ""))
        }?["info_hash"] as? String
    }

    func hasDownloadTask(_ hash: String) -> Bool {
        downloads().contains { ($0["info_hash"] as? String) == hash }
    }

    func failedUndismissedJobId() -> String? {
        jobs().first { $0["status"] as? String == "failed" && ($0["dismissed_at"] == nil || $0["dismissed_at"] is NSNull) }?["id"] as? String
    }

    func jobDismissed(_ id: String) -> Bool? {
        guard let job = request("GET", "/jobs/\(id)") as? [String: Any] else { return nil }
        return !(job["dismissed_at"] == nil || job["dismissed_at"] is NSNull)
    }

    func sessionCount() -> Int {
        (request("GET", "/sessions?limit=200") as? [Any])?.count ?? 0
    }
}
