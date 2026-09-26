import XCTest

/// 播放器端到端验收：打开播放器 → 出画且进度前进 → 暂停 → 退出，再到后端核对续播点已记录。
///
/// 两种引擎各跑一遍：系统播放器（AVPlayer，挑一部 MP4 原文件直出）与 MPV（挑一部 MKV，libmpv 直出原文件）。
/// 片子由测试在服务器上现找（先按容器挑，找不到就跳过），不写死条目 id。
/// 测完把这部片的续播点恢复成测试前的值，尽量不改动服务器上的真实观看记录。
/// 安全约束：每次点控件前都断言它 isHittable（被遮住就让用例失败，绝不按坐标误点下层）；
/// 不点任何会改服务器配置的按钮（软转同意弹窗的「开启并播放」等）。
///
/// 服务器与账号通过环境变量传入（xcodebuild 需加 TEST_RUNNER_ 前缀）：
///   MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD；可选 MC_SHOT_DIR（把关键界面截图写到这个目录）
///
/// 片源可直接指定，免去现找：MC_TEST_MP4_ITEM / MC_TEST_MKV_ITEM（电影条目 id）、MC_TEST_EPISODE_SHOW（剧集条目 id）。
/// 现找会逐个打开条目详情，而服务端打开详情会做起播预热（读片子）——对着真实片库跑时，
/// 一轮测试曾打开上百个详情。所以现找结果在整个测试进程里只找一次（见 Found），能指定就指定。
final class PlayerUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["MC_TEST_SERVER"] ?? "http://localhost:3000" }
    private var username: String { env["MC_TEST_USERNAME"] ?? "admin" }
    private var password: String { env["MC_TEST_PASSWORD"] ?? "mclaw-dev-2026" }

    /// 起播位置（秒）：固定从片长 10% 处开始，不受之前续播点影响。
    /// 不能太靠前——服务端按 Jellyfin 口径，片长 5% 以内的位置不记续播点（记为 0）
    private var startSeconds = 600

    // MARK: - 用例

    @MainActor
    func testSystemPlayerPlaysMP4AndRecordsResume() throws {
        let (item, duration) = try XCTUnwrap(try findMovie(container: "mp4"), "服务器上没有找到 MP4 电影")
        startSeconds = duration / 10
        try runPlayback(item: item, engine: "system", expectEngine: "系统播放器（AVPlayer）", shotPrefix: "avplayer")
    }

    @MainActor
    func testMPVPlaysMKVAndRecordsResume() throws {
        let (item, duration) = try XCTUnwrap(try findMovie(container: "mkv"), "服务器上没有找到 MKV 电影")
        startSeconds = duration / 10
        try runPlayback(item: item, engine: "mpv", expectEngine: "MPV（libmpv）", shotPrefix: "mpv")
    }

    /// 中央三键（后退 10 秒 / 播放暂停 / 前进 10 秒）：在播且控制层可见时必须存在、可点（对等审计 P-1）。
    /// 两种引擎各测一遍；不开诊断面板（与普通观看一致），开播到退出控制在 30 秒内。
    @MainActor
    func testCenterControlsWithSystemPlayer() throws {
        let (item, duration) = try XCTUnwrap(try findMovie(container: "mp4"), "服务器上没有找到 MP4 电影")
        startSeconds = duration / 10
        try runCenterControls(item: item, engine: "system", shotPrefix: "center-avplayer")
    }

    /// MPV 在模拟器上走 OpenGL ES、主线程逐帧绘制，XCUITest 每步都慢到跨过控制层 4 秒自动收起，
    /// 所以这一条打开诊断面板把控制层钉住（判别式不变：控制层可见时中央键必须在）。
    /// 可用 MC_TEST_MPV_ITEM 指定一部码率低些的 MKV（片子由测试现找时取第一部单文件 MKV）
    @MainActor
    func testCenterControlsWithMPV() throws {
        if let raw = env["MC_TEST_MPV_ITEM"], let item = Int(raw) {
            startSeconds = 600
            try runCenterControls(item: item, engine: "mpv", shotPrefix: "center-mpv", pinChrome: true)
            return
        }
        let (item, duration) = try XCTUnwrap(try findMovie(container: "mkv"), "服务器上没有找到 MKV 电影")
        startSeconds = duration / 10
        try runCenterControls(item: item, engine: "mpv", shotPrefix: "center-mpv", pinChrome: true)
    }

    @MainActor
    private func runCenterControls(item: Int, engine: String, shotPrefix: String, pinChrome: Bool = false) throws {
        continueAfterFailure = false
        let before = try resume(item)
        // 用例中途失败也要恢复续播点（continueAfterFailure=false 时 defer 不一定执行）
        addTeardownBlock { [self] in try? restoreResume(item, positionMs: before) }
        let app = launch(item: item, engine: engine, diagnostics: pinChrome)
        XCTAssertTrue(waitForPosition(app, atLeast: startSeconds + 1, timeout: 20), "进度没有前进（引擎 \(engine)）")

        // 判别式：控制层可见（底栏时间在）时，中央三键必须同时在。
        // 控制层 4 秒无操作会自己收起，所以用**一次快照**同时查时间与三键（query.count 只取一次界面树），
        // 查到控制层收起了就点画面唤出再查
        let playPause = app.buttons["player-play-pause"]
        let chromeIds = ["player-time", "后退 10 秒", "player-play-pause", "前进 10 秒"]
        let chromeQuery = app.descendants(matching: .any).matching(NSPredicate(format: "identifier IN %@", chromeIds))
        var visibleCount = 0
        for _ in 0 ..< 4 {
            visibleCount = chromeQuery.count
            if visibleCount > 0 { break }
            tapEmptyArea(app, dx: 0.5, dy: 0.7)
        }
        XCTAssertEqual(visibleCount, chromeIds.count, "控制层可见、正在播放时，时间与中央三键应同时在（实际只找到 \(visibleCount) 个）")
        shot(app, "\(shotPrefix)-playing")

        // 点中央键暂停：先让控制层「刚刚唤出」，留满 4 秒再点（点击前 tapControl 断言 isHittable）
        freshChrome(app)
        tapControl(app, "player-play-pause")
        wait(for: [expectation(for: NSPredicate(format: "label == %@", "播放"), evaluatedWith: playPause)], timeout: 5)
        // 暂停时控制层钉住不自动收起（P-7），三键一直在且可点
        sleep(5)
        for identifier in ["后退 10 秒", "player-play-pause", "前进 10 秒"] {
            let button = app.buttons[identifier]
            XCTAssertTrue(button.exists && button.isHittable, "暂停 5 秒后中央键 \(identifier) 不在或被遮挡")
        }
        shot(app, "\(shotPrefix)-paused")
        // 再点一次恢复播放
        tapControl(app, "player-play-pause")
        wait(for: [expectation(for: NSPredicate(format: "label == %@", "暂停"), evaluatedWith: playPause)], timeout: 5)
        tapControl(app, "player-close")
        XCTAssertTrue(app.descendants(matching: .any)["player-screen"].waitForNonExistence(timeout: 10), "播放器没有关闭")
    }

    /// 横屏键 → 横屏布局与锁屏 → 解锁 → 左上角「退出横屏」回到竖屏
    @MainActor
    func testLandscapeAndLock() throws {
        let (item, duration) = try XCTUnwrap(try findMovie(container: "mp4"), "服务器上没有找到 MP4 电影")
        startSeconds = duration / 10
        let before = try resume(item)
        defer { try? restoreResume(item, positionMs: before) }
        // 打开诊断面板：面板开着时控制条不自动隐藏，免得「刚确认按钮在、点下去时已隐藏」的竞态
        let app = launch(item: item, engine: "system", diagnostics: true)
        XCTAssertTrue(waitForPosition(app, atLeast: startSeconds + 2, timeout: 90), "进度没有前进")

        tapControl(app, "player-横屏")
        let window = app.windows.firstMatch
        let rotated = NSPredicate { _, _ in window.frame.width > window.frame.height }
        wait(for: [expectation(for: rotated, evaluatedWith: nil)], timeout: 10)
        revealChrome(app)
        shot(app, "landscape")

        tapControl(app, "player-lock")
        XCTAssertFalse(app.buttons["player-play-pause"].exists, "锁屏后控制层应隐藏")
        tapEmptyArea(app, dx: 0.5, dy: 0.5)
        XCTAssertTrue(app.buttons["player-unlock"].waitForExistence(timeout: 3), "锁屏时点画面应出现解锁键")
        shot(app, "landscape-locked")
        tapControl(app, "player-unlock")

        tapControl(app, "player-close") // 横屏时是「退出横屏」
        let portrait = NSPredicate { _, _ in window.frame.width < window.frame.height }
        wait(for: [expectation(for: portrait, evaluatedWith: nil)], timeout: 10)
        XCTAssertTrue(app.descendants(matching: .any)["player-screen"].exists, "退出横屏不应关闭播放器")
        tapControl(app, "player-close")
        XCTAssertTrue(app.descendants(matching: .any)["player-screen"].waitForNonExistence(timeout: 10), "播放器没有关闭")
    }

    /// 剧集片尾 40 秒内出现「即将播放」卡片 → 点「立即播放」→ 换到下一集并开始播放
    @MainActor
    func testUpNextPlaysNextEpisode() throws {
        let (show, current, next, durationMs) = try XCTUnwrap(try findEpisodePair(), "服务器上没有找到同季相邻两集都在位的剧集")
        let beforeCurrent = try resumeState(show, 1, current)
        let beforeNext = try resumeState(show, 1, next)
        defer {
            // 恢复两集的观看状态（片尾附近会被判为已看）
            try? restoreEpisode(show, current, beforeCurrent)
            try? restoreEpisode(show, next, beforeNext)
        }
        startSeconds = durationMs / 1000 - 30
        let app = XCUIApplication()
        app.launchArguments = [
            "-mcServer", server, "-mcUser", username, "-mcPass", password,
            "-mcRoute", String(format: "/play/%d/s01e%02d?t=%d", show, current, startSeconds),
            // 用系统播放器：模拟器上 MPV 走 OpenGL ES 在主线程逐帧绘制，软解高码率时主线程一直忙，
            // XCUITest 每一步都要等「App 空闲」而被拖到超时；这条用例测的是切集逻辑，与引擎无关
            "-movieclaw.player.engine", "system",
        ]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["player-upnext"].waitForExistence(timeout: 60), "片尾没有出现「即将播放」卡片")
        shot(app, "upnext")
        tapControl(app, "upnext-play")
        // 换集后播放头回到下一集开头（它从没看过，续播点是 0）
        var switched = false
        for _ in 0 ..< 60 {
            if let current = position(app), current < 60 { switched = true; break }
            sleep(1)
        }
        if !switched { print("[PlayerUITests] 界面树：\n\(app.debugDescription)") }
        XCTAssertTrue(switched, "没有切到下一集")
        revealChrome(app)
        let nextLabel = app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH %@", String(format: "S01E%02d", next))).firstMatch
        XCTAssertTrue(nextLabel.waitForExistence(timeout: 5), "顶栏没有显示下一集")
        XCTAssertTrue(waitForPosition(app, atLeast: 3, timeout: 90), "下一集没有开始播放")
        tapControl(app, "player-close")
        XCTAssertTrue(app.descendants(matching: .any)["player-screen"].waitForNonExistence(timeout: 10), "播放器没有关闭")
    }

    @MainActor
    private func launch(item: Int, engine: String, diagnostics: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = [
            "-mcServer", server, "-mcUser", username, "-mcPass", password,
            "-mcRoute", "/play/\(item)/s00e00?t=\(startSeconds)",
            "-movieclaw.player.engine", engine,
            "-mcPlayerDiagnostics", diagnostics ? "YES" : "NO",
        ]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["player-screen"].waitForExistence(timeout: 30), "播放器没有打开")
        return app
    }

    // MARK: - 流程

    @MainActor
    private func runPlayback(item: Int, engine: String, expectEngine: String, shotPrefix: String) throws {
        continueAfterFailure = false
        let before = try resume(item)
        defer { try? restoreResume(item, positionMs: before) }

        let app = launch(item: item, engine: engine, diagnostics: true)
        // 诊断面板里的引擎行：确认真的是这个引擎在放
        let engineLabel = app.staticTexts[expectEngine]
        XCTAssertTrue(engineLabel.waitForExistence(timeout: 40), "引擎不是 \(expectEngine)")

        // 出画且进度前进：播放头越过起播点 3 秒
        let advanced = waitForPosition(app, atLeast: startSeconds + 3, timeout: 90)
        XCTAssertTrue(advanced, "进度没有前进（引擎 \(engine)）")
        shot(app, "\(shotPrefix)-playing")

        // 暂停
        tapControl(app, "player-play-pause")
        sleep(2)
        let paused = position(app)
        sleep(2)
        XCTAssertEqual(position(app), paused, "暂停后进度仍在走")
        XCTAssertTrue(app.descendants(matching: .any)["player-paused"].waitForExistence(timeout: 5) || app.staticTexts["已暂停"].exists, "没有暂停遮罩")
        shot(app, "\(shotPrefix)-paused")

        // 菜单截图（字幕、设置）
        tapControl(app, "player-字幕")
        XCTAssertTrue(app.buttons["subtitle-off"].waitForExistence(timeout: 5), "字幕菜单没有打开")
        shot(app, "\(shotPrefix)-subtitles")
        tapControl(app, "player-设置")
        XCTAssertTrue(app.buttons["engine-\(engine)"].waitForExistence(timeout: 5), "设置菜单没有打开")
        shot(app, "\(shotPrefix)-settings")
        // 点画面空白处收起菜单
        tapEmptyArea(app, dx: 0.95, dy: 0.55)
        XCTAssertTrue(app.buttons["engine-\(engine)"].waitForNonExistence(timeout: 3), "点画面应收起菜单")

        // 退出
        tapControl(app, "player-close")
        XCTAssertTrue(app.descendants(matching: .any)["player-screen"].waitForNonExistence(timeout: 10), "播放器没有关闭")

        // 后端续播点：暂停时的位置（允许 ±5 秒误差）
        let expectedMs = (paused ?? startSeconds) * 1000
        var recorded = -1
        for _ in 0 ..< 10 {
            recorded = try resume(item)
            if abs(recorded - expectedMs) <= 5000 { break }
            sleep(1)
        }
        XCTAssertLessThanOrEqual(abs(recorded - expectedMs), 5000, "后端续播点 \(recorded)ms 与暂停位置 \(expectedMs)ms 不符")
    }

    // MARK: - 界面辅助

    /// 控制条 4 秒后自动隐藏：看不到时点一下画面唤出（点在画面中下部的空白处，避开中央按钮）
    @MainActor
    private func revealChrome(_ app: XCUIApplication) {
        if !app.staticTexts["player-time"].exists {
            tapEmptyArea(app, dx: 0.5, dy: 0.7)
        }
        _ = app.staticTexts["player-time"].waitForExistence(timeout: 3)
    }

    /// 让控制层处在「刚唤出」的状态：可见就先点一下收起、再点一下唤出，自动收起的 4 秒倒计时从头算
    @MainActor
    private func freshChrome(_ app: XCUIApplication) {
        if app.staticTexts["player-time"].exists {
            tapEmptyArea(app, dx: 0.5, dy: 0.7)
            _ = app.staticTexts["player-time"].waitForNonExistence(timeout: 3)
        }
        tapEmptyArea(app, dx: 0.5, dy: 0.7)
        _ = app.staticTexts["player-time"].waitForExistence(timeout: 3)
    }

    /// 点控制层上的按钮：控制条藏起来了就先点画面唤出（控制条 4 秒无操作自动隐藏）
    @MainActor
    private func tapControl(_ app: XCUIApplication, _ identifier: String) {
        let button = app.buttons[identifier]
        for _ in 0 ..< 3 {
            if button.waitForExistence(timeout: 1.5), button.isHittable {
                button.tap()
                return
            }
            // 控制条藏起来了：点画面唤出
            tapEmptyArea(app, dx: 0.5, dy: 0.7)
        }
        XCTFail("控件 \(identifier) 不存在或被遮挡（isHittable=false）")
    }

    /// 点画面空白处（唤出/收起控制层、锁屏时唤出解锁键）。
    /// 点之前确认落点处没有任何可点的按钮——只该落在手势层上，绝不盲点到下层控件
    @MainActor
    private func tapEmptyArea(_ app: XCUIApplication, dx: CGFloat, dy: CGFloat) {
        let point = app.windows.firstMatch.coordinate(withNormalizedOffset: CGVector(dx: dx, dy: dy))
        // 只查播放器里的按钮，且用一次快照查完：全屏播放器盖住了下层页面（逐个查下层几十个按钮在主线程忙时
        // 要好几分钟），控制层又会 4 秒自动收起（逐个取元素时按钮可能刚好消失，查询本身就失败）
        let root = try? app.descendants(matching: .any)["player-screen"].snapshot()
        let covering = root.flatMap { Self.buttons(in: $0).first { $0.frame.contains(point.screenPoint) } }
        XCTAssertNil(covering, "落点 (\(dx), \(dy)) 上有控件 \(covering?.identifier ?? "")，不能盲点")
        point.tap()
    }

    private static func buttons(in snapshot: XCUIElementSnapshot) -> [XCUIElementSnapshot] {
        (snapshot.elementType == .button ? [snapshot] : []) + snapshot.children.flatMap { buttons(in: $0) }
    }

    /// 当前播放头（秒），读自底栏时间标签的可访问性值
    @MainActor
    private func position(_ app: XCUIApplication) -> Int? {
        revealChrome(app)
        let label = app.staticTexts["player-time"]
        guard label.exists else { return nil }
        // 可访问性值是纯数字秒数；去掉千分位以防系统按地区格式化
        return (label.value as? String).flatMap { Int($0.replacingOccurrences(of: ",", with: "")) }
    }

    @MainActor
    private func waitForPosition(_ app: XCUIApplication, atLeast seconds: Int, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let current = position(app), current >= seconds { return true }
            sleep(1)
        }
        return false
    }

    @MainActor
    private func shot(_ app: XCUIApplication, _ name: String) {
        let screenshot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let dir = env["MC_SHOT_DIR"] {
            try? screenshot.pngRepresentation.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
    }

    // MARK: - 后端辅助（与 App 共用同一台服务器）

    /// 现找结果的进程级缓存：每个用例都是新实例，放实例上等于每条用例从头再扫一遍片库。
    /// 值为 nil 也缓存（「没找到」同样不必再扫）。
    @MainActor private enum Found {
        static var movies: [String: (Int, Int)?] = [:]
        static var episodePair: (Int, Int, Int, Int)??
    }

    private lazy var session: URLSession = {
        // ephemeral 自带一份内存 Cookie 存储，与 App 的登录态互不影响
        URLSession(configuration: .ephemeral)
    }()
    private var loggedIn = false

    private func call(_ method: String, _ path: String, body: [String: Any]? = nil) throws -> Any {
        if !loggedIn {
            loggedIn = true
            _ = try call("POST", "/auth/login", body: ["username": username, "password": password, "remember": false])
        }
        var request = URLRequest(url: URL(string: "\(server)/api/v1\(path)")!)
        request.httpMethod = method
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let semaphore = DispatchSemaphore(value: 0)
        nonisolated(unsafe) var result: Data?
        session.dataTask(with: request) { data, _, _ in
            result = data
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 30)
        let data = try XCTUnwrap(result, "请求 \(path) 没有响应")
        let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        guard json["success"] as? Bool == true else {
            throw NSError(domain: "PlayerUITests", code: 1, userInfo: [NSLocalizedDescriptionKey: "\(method) \(path) 失败：\(json["message"] ?? "")"])
        }
        return json["data"] ?? NSNull()
    }

    private func resume(_ item: Int) throws -> Int {
        let data = try call("GET", "/playback/resume?media_item_id=\(item)&season_number=0&episode_number=0") as? [String: Any]
        return data?["position_ms"] as? Int ?? 0
    }

    private func restoreResume(_ item: Int, positionMs: Int) throws {
        _ = try call("POST", "/playback/progress", body: [
            "media_item_id": item, "season_number": 0, "episode_number": 0,
            "event": "stop", "position_ms": positionMs, "device_id": "ui-test",
        ])
    }

    private func resumeState(_ item: Int, _ season: Int, _ episode: Int) throws -> [String: Any] {
        try call("GET", "/playback/resume?media_item_id=\(item)&season_number=\(season)&episode_number=\(episode)") as? [String: Any] ?? [:]
    }

    private func restoreEpisode(_ show: Int, _ episode: Int, _ state: [String: Any]) throws {
        _ = try call("POST", "/playback/progress", body: [
            "media_item_id": show, "season_number": 1, "episode_number": episode,
            "event": "stop", "position_ms": state["position_ms"] as? Int ?? 0, "device_id": "ui-test",
        ])
        _ = try call("POST", "/playback/marks", body: [
            "media_item_id": show, "season_number": 1, "episode_number": episode,
            "played": state["played"] as? Bool ?? false, "device_id": "ui-test",
        ])
    }

    /// 找一部第一季前两集都在位、且都是 1080p 以下 SDR 的剧集：返回 (条目, 本集, 下一集, 本集片长毫秒)。
    /// 避开 4K/HDR：模拟器没有硬解，4K HEVC 软解会把整台模拟器拖垮，测的就不是切集逻辑了
    @MainActor
    private func findEpisodePair() throws -> (Int, Int, Int, Int)? {
        if let cached = Found.episodePair { return cached }
        let found = try scanEpisodePair()
        Found.episodePair = .some(found)
        return found
    }

    private func scanEpisodePair() throws -> (Int, Int, Int, Int)? {
        let pinned = env["MC_TEST_EPISODE_SHOW"].flatMap(Int.init)
        let libraries = try call("GET", "/libraries") as? [[String: Any]] ?? []
        for library in libraries where library["kind"] as? String == "tv" {
            guard let id = library["id"] as? Int else { continue }
            let items: [[String: Any]] = if let pinned {
                [["media_item_id": pinned]]
            } else {
                try call("GET", "/libraries/\(id)/items?limit=30") as? [[String: Any]] ?? []
            }
            for item in items {
                guard let show = item["media_item_id"] as? Int,
                      let detail = try? call("GET", "/libraries/\(id)/items/\(show)") as? [String: Any],
                      let files = detail["files"] as? [[String: Any]] else { continue }
                let light = files.filter {
                    $0["season_number"] as? Int == 1 && $0["hdr"] is NSNull
                        && ["1080p", "720p"].contains($0["resolution"] as? String ?? "")
                        && ($0["duration_seconds"] as? Int ?? 0) > 120
                }
                let numbers = Set(light.compactMap { $0["episode_number"] as? Int }).sorted()
                guard let first = numbers.first, numbers.contains(first + 1),
                      let duration = light.first(where: { $0["episode_number"] as? Int == first })?["duration_seconds"] as? Int
                else { continue }
                return (show, first, first + 1, duration * 1000)
            }
        }
        return nil
    }

    /// 在电影库里找一部主文件是指定容器的片子（最多翻前 60 部）；MC_TEST_<容器>_ITEM 可直接指定
    @MainActor
    private func findMovie(container: String) throws -> (Int, Int)? {
        if let cached = Found.movies[container] { return cached }
        let found = try scanMovie(container: container)
        Found.movies[container] = .some(found)
        return found
    }

    private func scanMovie(container: String) throws -> (Int, Int)? {
        let pinned = env["MC_TEST_\(container.uppercased())_ITEM"].flatMap(Int.init)
        let libraries = try call("GET", "/libraries") as? [[String: Any]] ?? []
        for library in libraries where library["kind"] as? String == "movie" {
            guard let id = library["id"] as? Int else { continue }
            let items: [[String: Any]] = if let pinned {
                [["media_item_id": pinned, "file_count": 1]]
            } else {
                try call("GET", "/libraries/\(id)/items?limit=60") as? [[String: Any]] ?? []
            }
            // 列表已带文件数：多文件条目不必打开详情就能排除
            for item in items where item["file_count"] as? Int == 1 {
                guard let itemId = item["media_item_id"] as? Int,
                      let detail = try? call("GET", "/libraries/\(id)/items/\(itemId)") as? [String: Any],
                      let files = detail["files"] as? [[String: Any]], files.count == 1,
                      files[0]["container"] as? String == container,
                      let duration = files[0]["duration_seconds"] as? Int, duration > 600 else { continue }
                return (itemId, duration)
            }
        }
        return nil
    }
}
