import XCTest

/// AI 会话模块的端到端验收。
///
/// 服务器是用户正在使用的真实 MovieClaw，智能体有真实的写能力（订阅、下载、改配置），因此硬规则：
/// - 每次点击前都经 `safeTap`：断言目标存在、`isHittable`、且不与底部标签栏重叠，否则用例直接失败；
/// - 用户已有的历史会话只读展示：不发消息、不改写、不重命名、不删除（历史会话里连「改写这条提问」都不点）；
/// - 只有 `testLiveSessionStreamRetryAndStop` 会真实发消息（最多 2 条、都是明确写了「只查询」的只读问题），
///   它会消耗模型额度，默认跳过，显式设置 `MC_ALLOW_AGENT_LIVE=1` 才跑；结束后删除它自己创建的会话；
/// - 新任务页的菜单用例只打开菜单、不发送。
///
/// 环境变量（scripts/test.sh 以 TEST_RUNNER_ 前缀转交）：MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD；
/// 截图落盘目录 MC_SHOT_DIR（可选）。
final class AgentUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["MC_TEST_SERVER"] ?? "http://localhost:3000" }
    private var username: String { env["MC_TEST_USERNAME"] ?? "admin" }
    private var password: String { env["MC_TEST_PASSWORD"] ?? "mclaw-dev-2026" }

    private lazy var backend = AgentBackend(server: server, username: username, password: password)
    /// 本用例创建的会话（含派生出的会话），tearDown 里删除
    private var createdSessionIds: [String] = []

    override func tearDown() {
        for id in createdSessionIds.reversed() {
            // 运行中的会话服务端拒绝删除：等它结束（最多 2 分钟）
            for _ in 0 ..< 60 {
                let summary = backend.request("GET", "/sessions/\(id)") as? [String: Any]
                if (summary?["session"] as? [String: Any])?["running"] as? Bool != true { break }
                Thread.sleep(forTimeInterval: 2)
            }
            backend.request("DELETE", "/sessions/\(id)")
            print("已删除测试会话 \(id)")
        }
        super.tearDown()
    }

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

    /// 唯一的点击入口：存在、可点、未被标签栏遮挡，才点
    @MainActor
    private func safeTap(_ app: XCUIApplication, _ target: XCUIElement, _ what: String, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(target.waitForExistence(timeout: 15), "找不到「\(what)」", file: file, line: line)
        XCTAssertTrue(target.isHittable, "「\(what)」不可点（被遮挡或在屏幕外）", file: file, line: line)
        let bar = app.tabBars.firstMatch
        if bar.exists, bar.isHittable {
            XCTAssertFalse(bar.frame.intersects(target.frame), "「\(what)」与底部标签栏重叠，拒绝按坐标点击", file: file, line: line)
        }
        target.tap()
    }

    /// 点气泡的文字区（右下角）：气泡中央可能是图片缩略图，点中会打开灯箱
    @MainActor
    private func tapBubbleText(_ app: XCUIApplication, _ bubble: XCUIElement) {
        XCTAssertTrue(bubble.waitForExistence(timeout: 15), "找不到提问气泡")
        XCTAssertTrue(bubble.isHittable, "提问气泡不可点")
        bubble.coordinate(withNormalizedOffset: CGVector(dx: 0.85, dy: 0.93)).tap()
    }

    /// 把目标轻扫到底部输入区之上（最多 10 次），避免按坐标点到输入框
    @MainActor
    private func scrollAboveComposer(_ app: XCUIApplication, _ target: XCUIElement) {
        let composerTop = element(app, "agent-composer-input").frame.minY - 60
        for _ in 0 ..< 10 {
            if target.isHittable, target.frame.maxY < composerTop, target.frame.minY > 120 { return }
            let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.6))
            start.press(forDuration: 0.05, thenDragTo: app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.4)))
        }
        XCTAssertLessThan(target.frame.maxY, composerTop, "目标被底部输入区遮挡")
    }

    /// 点空白处收起弹层（点在顶栏下方的空白区，弹层都锚在底部输入框上方，不会点进弹层）
    @MainActor
    private func dismissPopover(_ app: XCUIApplication, _ panel: XCUIElement) {
        for _ in 0 ..< 3 where panel.exists {
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.14)).tap()
            _ = panel.waitForNonExistence(timeout: 2)
        }
        XCTAssertFalse(panel.exists, "弹层没有收起")
    }

    /// 沉浸式：会话页与新任务页不显示标签栏
    @MainActor
    private func assertTabBarHidden(_ app: XCUIApplication) {
        let bar = app.tabBars.firstMatch
        XCTAssertFalse(bar.exists && bar.isHittable, "沉浸页不应显示标签栏")
    }

    // MARK: 历史会话只读展示

    /// 选一个内容丰富的历史会话（有行内媒体卡片，最好还带图片），只读展示：
    /// 转录、处理过程折叠、工具调用展开、气泡操作浮现、回到最新消息按钮。
    @MainActor
    func testHistorySessionReadOnlyShowcase() throws {
        let id = try XCTUnwrap(backend.richSessionId(), "服务器上没有带媒体卡片的历史会话，无法做只读展示")
        let app = launch(route: "/sessions/\(id)")

        let bubble = app.descendants(matching: .any).matching(identifier: "agent-user-bubble").firstMatch
        XCTAssertTrue(bubble.waitForExistence(timeout: 30), "会话转录没有加载出来")
        assertTabBarHidden(app)
        XCTAssertTrue(element(app, "agent-composer-input").exists, "底部输入框缺失")
        XCTAssertTrue(element(app, "agent-send").exists, "发送键缺失")
        snapshot("agent-history-bottom")

        // 卡片组（生成式 UI）
        XCTAssertTrue(element(app, "agent-media-cards").waitForExistence(timeout: 20), "行内媒体卡片没有渲染")

        // 回到顶部：离开底部后出现「回到最新消息」
        let scroll = app.scrollViews.firstMatch
        for _ in 0 ..< 25 { scroll.swipeDown(velocity: .fast) }
        snapshot("agent-history-top")
        let latest = element(app, "agent-scroll-latest")
        XCTAssertTrue(latest.waitForExistence(timeout: 5), "离开底部后应出现「回到最新消息」")

        // 首条提问：点气泡浮现复制键（历史会话不点改写）
        let firstBubble = app.descendants(matching: .any).matching(identifier: "agent-user-bubble").firstMatch
        tapBubbleText(app, firstBubble)
        XCTAssertTrue(element(app, "agent-copy-message").waitForExistence(timeout: 5), "点气泡后应浮现复制键")

        // 处理过程折叠块 → 展开 → 展开一次工具调用
        let toggle = element(app, "agent-process-toggle")
        safeTap(app, toggle, "处理过程折叠块")
        let tool = element(app, "agent-tool-row")
        XCTAssertTrue(tool.waitForExistence(timeout: 5), "展开处理过程后应列出工具调用")
        scrollAboveComposer(app, tool)
        safeTap(app, tool, "工具调用行")
        snapshot("agent-history-tool-expanded")

        // 逐屏往下截图（对照网页）
        for i in 0 ..< 6 {
            scroll.swipeUp(velocity: .slow)
            snapshot("agent-history-page-\(i)")
        }

        safeTap(app, element(app, "agent-scroll-latest"), "回到最新消息")
        let copyAnswer = app.descendants(matching: .any).matching(identifier: "agent-copy-answer").element(boundBy: 0)
        XCTAssertTrue(copyAnswer.waitForExistence(timeout: 10), "轮次页脚缺少「复制」")
    }

    // MARK: 新任务页的菜单（不发送）

    @MainActor
    func testNewSessionComposerMenus() {
        let app = launch(route: "/new")
        let input = element(app, "agent-composer-input")
        XCTAssertTrue(input.waitForExistence(timeout: 20), "新任务页缺少输入框")
        XCTAssertTrue(app.navigationBars["新会话"].waitForExistence(timeout: 5), "顶栏标题应为「新会话」")
        assertTabBarHidden(app)
        XCTAssertFalse(element(app, "agent-send").isEnabled, "空输入时发送键应不可用")

        // 「+」菜单：上传图片 + 使用技能
        safeTap(app, element(app, "agent-composer-plus"), "加号菜单")
        XCTAssertTrue(app.buttons["agent-pick-image"].waitForExistence(timeout: 5), "加号菜单缺少「照片图库」")
        XCTAssertTrue(app.buttons["agent-pick-file"].exists, "加号菜单缺少「选取文件」（同 Web 系统选择器的三种来源）")
        XCTAssertTrue(app.staticTexts["使用技能"].exists, "加号菜单缺少「使用技能」")
        let skill = app.buttons.matching(NSPredicate(format: "label BEGINSWITH '⚡'")).firstMatch
        var pickedSkill = false
        if skill.waitForExistence(timeout: 8) {
            snapshot("agent-new-plus-menu")
            safeTap(app, skill, "技能项")
            pickedSkill = true
        } else {
            snapshot("agent-new-plus-menu")
            dismissPopover(app, element(app, "agent-plus-menu"))
        }
        if pickedSkill {
            XCTAssertTrue(app.buttons.matching(NSPredicate(format: "label BEGINSWITH '移除技能'")).firstMatch.waitForExistence(timeout: 5), "选中技能后输入框上方应出现技能 chip")
            XCTAssertTrue(element(app, "agent-send").isEnabled, "有技能时发送键应可用")
        }

        // 模型菜单：模型清单 + 思维链强度
        let model = element(app, "agent-model-menu")
        if model.waitForExistence(timeout: 8) {
            safeTap(app, model, "模型菜单")
            XCTAssertTrue(element(app, "agent-model-panel").waitForExistence(timeout: 5), "模型菜单没有打开")
            let thinking = app.staticTexts["思维链强度"].exists || app.staticTexts["思维链"].exists || app.staticTexts["该模型不支持调节思考强度"].exists
            XCTAssertTrue(thinking, "模型菜单下半缺少思维链控件")
            if app.staticTexts["思维链强度"].exists {
                XCTAssertTrue(app.staticTexts["更快"].exists && app.staticTexts["更聪明"].exists, "滑杆缺少「更快 / 更聪明」轴标签")
            }
            snapshot("agent-new-model-menu")
            dismissPopover(app, element(app, "agent-model-panel"))
        }

        // 「/」技能快选（有技能时弹出）
        safeTap(app, input, "输入框")
        input.typeText("/")
        if pickedSkill {
            XCTAssertTrue(element(app, "agent-slash-menu").waitForExistence(timeout: 8), "输入「/」应弹出技能快选")
        }
        snapshot("agent-new-slash")
    }

    // MARK: 实时流（真实发消息，默认跳过）

    /// 新任务 → 首条消息创建会话并替换路由到会话页 → 实时流（执行中… → 完成）→
    /// 改写这条提问「替换并重新提问」→ 停止。共发 2 条只读消息，结束后删除本会话。
    @MainActor
    func testLiveSessionStreamRetryAndStop() throws {
        try XCTSkipUnless(env["MC_ALLOW_AGENT_LIVE"] == "1", "会消耗模型额度，需显式设置 MC_ALLOW_AGENT_LIVE=1")
        let marker = String(UUID().uuidString.prefix(6))
        let question = "T\(marker) 只查询，不要做任何修改、订阅、下载或删除：列出媒体库最近入库的 3 部电影"

        let app = launch(route: "/new")
        let input = element(app, "agent-composer-input")
        XCTAssertTrue(input.waitForExistence(timeout: 20))
        safeTap(app, input, "输入框")
        input.typeText(question)
        safeTap(app, element(app, "agent-send"), "发送")

        // 创建会话后替换路由：顶栏不再是「新会话」，出现用户气泡与执行中
        XCTAssertTrue(app.descendants(matching: .any).matching(identifier: "agent-user-bubble").firstMatch.waitForExistence(timeout: 30), "发送后没有进入会话页")
        let sessionId = try XCTUnwrap(backend.sessionId(containing: marker), "服务端没有找到本用例创建的会话")
        createdSessionIds.append(sessionId)
        print("创建测试会话 \(sessionId)：\(question)")
        XCTAssertTrue(element(app, "agent-turn-running").waitForExistence(timeout: 20), "运行中应显示进度与实时耗时")
        assertTabBarHidden(app)
        snapshot("agent-live-running")

        let done = element(app, "agent-copy-answer")
        XCTAssertTrue(done.waitForExistence(timeout: 300), "五分钟内没有等到本轮完成")
        snapshot("agent-live-done")

        // 返回键应直接回到进入新任务前的页面（/new 已被替换，不叠两层）
        XCTAssertFalse(app.navigationBars["新会话"].exists)

        // 改写这条提问 → 替换并重新提问 → 立即停止
        let bubble = app.descendants(matching: .any).matching(identifier: "agent-user-bubble").firstMatch
        tapBubbleText(app, bubble)
        safeTap(app, element(app, "agent-edit-message"), "改写这条提问")
        XCTAssertTrue(app.staticTexts["正在改写较早的提问，发送后将替换其后的对话"].waitForExistence(timeout: 5), "改写态横幅缺失")
        safeTap(app, input, "输入框")
        input.typeText("（改写：只列 1 部）")
        safeTap(app, element(app, "agent-send"), "发送改写")
        let confirm = app.alerts.buttons["替换并重新提问"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 5), "改写发送前应二次确认")
        confirm.tap()
        let stop = element(app, "agent-stop")
        if stop.waitForExistence(timeout: 15), stop.isHittable {
            snapshot("agent-live-retry-running")
            stop.tap()
            let stopped = element(app, "agent-turn-stopped")
            XCTAssertTrue(stopped.waitForExistence(timeout: 60) || element(app, "agent-copy-answer").exists, "停止后应显示「已停止」")
        }
        snapshot("agent-live-stopped")

        // 派生（不启动模型）：新会话顶部是来源卡片「已从…续接上下文」
        let forked = try XCTUnwrap(backend.fork(sessionId), "派生会话失败")
        createdSessionIds.append(forked)
        print("派生测试会话 \(forked)（来源 \(sessionId)）")
        app.terminate()
        let forkedApp = launch(route: "/sessions/\(forked)")
        XCTAssertTrue(element(forkedApp, "agent-handoff").waitForExistence(timeout: 30), "派生会话缺少来源卡片")
        snapshot("agent-live-handoff")
    }
}

/// 直连后端的只读查询（选历史会话、找本用例创建的会话）与清理
private final class AgentBackend {
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

    private func sessions() -> [[String: Any]] {
        request("GET", "/sessions?limit=50") as? [[String: Any]] ?? []
    }

    /// 有 show_media_cards 卡片（优先同时带图片附件）的历史会话
    func richSessionId() -> String? {
        var fallback: String?
        for summary in sessions() where summary["running"] as? Bool != true {
            guard let id = summary["id"] as? String,
                  let detail = request("GET", "/sessions/\(id)") as? [String: Any],
                  let entries = detail["entries"] as? [[String: Any]] else { continue }
            var cards = false
            var image = false
            for entry in entries {
                guard let message = entry["message"] as? [String: Any] else { continue }
                for call in message["tool_calls"] as? [[String: Any]] ?? [] where (call["name"] as? String)?.hasPrefix("show_media_cards") == true {
                    cards = true
                }
                for part in message["content"] as? [[String: Any]] ?? [] where part["type"] as? String == "image" {
                    image = true
                }
            }
            if cards, image { return id }
            if cards, fallback == nil { fallback = id }
        }
        return fallback
    }

    /// 从源会话派生独立新会话（不触发模型运行），返回新会话 id
    func fork(_ id: String) -> String? {
        ((request("POST", "/sessions/\(id)/fork") as? [String: Any])?["session"] as? [String: Any])?["id"] as? String
    }

    /// 本用例创建的会话（按提问里的随机标记找）
    func sessionId(containing marker: String) -> String? {
        for _ in 0 ..< 10 {
            if let hit = sessions().first(where: { ($0["last_prompt"] as? String ?? "").contains(marker) || ($0["title"] as? String ?? "").contains(marker) }) {
                return hit["id"] as? String
            }
            Thread.sleep(forTimeInterval: 1)
        }
        return nil
    }
}
