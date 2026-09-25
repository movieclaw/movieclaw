import XCTest

/// 媒体库模块的端到端验收：首页 → 单库海报墙（分页、筛选入口）→ 条目详情（收藏、标已看、加入合集）
/// → 自定义首页（显隐自动保存）。每个写操作都到后端核对状态，测完恢复原状。
///
/// 依赖一台真实运行的 MovieClaw。通过环境变量指定（xcodebuild 需加 TEST_RUNNER_ 前缀传入，
/// scripts/test.sh 会转交）：MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD。
/// 服务器上至少要有一个可浏览、带影片的媒体库。
final class LibraryUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["MC_TEST_SERVER"] ?? "http://localhost:3000" }
    private var username: String { env["MC_TEST_USERNAME"] ?? "admin" }
    private var password: String { env["MC_TEST_PASSWORD"] ?? "mclaw-dev-2026" }

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
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    /// 测试侧直连后端核对状态（与 App 用同一套会话 Cookie 机制，独立登录）
    private lazy var backend = Backend(server: server, username: username, password: password)

    /// 首页 → 第一个库 → 第一部作品，返回作品的 (libraryId, itemId)
    @MainActor
    private func openFirstItem(_ app: XCUIApplication) -> (Int, Int) {
        let card = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'library-card-'")).firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 20), "首页应列出媒体库卡片")
        let libraryId = Int(card.identifier.replacingOccurrences(of: "library-card-", with: "")) ?? 0
        card.tap()
        let wall = app.descendants(matching: .any)["poster-wall"]
        XCTAssertTrue(wall.waitForExistence(timeout: 20), "单库页应出现海报墙")
        let first = wall.buttons.firstMatch
        XCTAssertTrue(first.waitForExistence(timeout: 10))
        first.tap()
        XCTAssertTrue(app.staticTexts["item-title"].waitForExistence(timeout: 20), "应进入条目详情")
        // 详情页把条目 id 挂在标题的无障碍值上，测试据此到后端核对
        let itemId = Int(app.staticTexts["item-title"].value as? String ?? "") ?? 0
        return (libraryId, itemId)
    }

    // MARK: 用例

    /// 首页三块内容与跳转：统计行、我的媒体库卡片、全部合集、自定义首页入口
    @MainActor
    func testHomeShowsRowsAndNavigates() {
        let app = launch(route: "/library")
        XCTAssertTrue(app.staticTexts["library-stats"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["library-stats"].label.contains("个媒体库"))
        snapshot("媒体库首页")
        app.buttons["library-customize"].tap()
        XCTAssertTrue(app.staticTexts["customize-summary"].waitForExistence(timeout: 15))
        snapshot("自定义首页")
    }

    /// 单库海报墙：滚到底自动加载下一页（窗口进度文案随之前进）
    @MainActor
    func testWallPaginates() throws {
        let app = launch(route: "/library")
        let card = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'library-card-'")).firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 20))
        let libraryId = Int(card.identifier.replacingOccurrences(of: "library-card-", with: "")) ?? 0
        let total = backend.itemCount(libraryId: libraryId)
        card.tap()
        XCTAssertTrue(app.descendants(matching: .any)["poster-wall"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["library-filter-button"].exists, "筛选入口应在墙顶")
        try XCTSkipIf(total <= 60, "这个库不足一页（\(total) 部），跳过分页验证")
        let progressed = app.staticTexts.matching(NSPredicate(format: "label CONTAINS '第 1–120 部'")).firstMatch
        for _ in 0 ..< 40 where !progressed.exists {
            app.swipeUp(velocity: .fast)
        }
        XCTAssertTrue(progressed.waitForExistence(timeout: 10), "第二页应被自动加载")
        snapshot("海报墙第二页")
    }

    /// 条目详情：收藏与标已看都落到后端，测完恢复
    @MainActor
    func testFavoriteAndPlayedMarks() {
        let app = launch(route: "/library")
        let (_, itemId) = openFirstItem(app)
        XCTAssertGreaterThan(itemId, 0)
        let wasFavorite = backend.marks(itemId: itemId)?["is_favorite"] as? Bool ?? false

        let favorite = app.buttons["item-favorite"]
        XCTAssertTrue(favorite.waitForExistence(timeout: 15))
        favorite.tap()
        XCTAssertTrue(waitUntil { self.backend.marks(itemId: itemId)?["is_favorite"] as? Bool == !wasFavorite }, "收藏状态应写到后端")
        snapshot("切换收藏后")
        favorite.tap()
        XCTAssertTrue(waitUntil { self.backend.marks(itemId: itemId)?["is_favorite"] as? Bool == wasFavorite }, "再点一次应恢复原状")

        // 有续播进度的片不动已看标记（标已看再取消会清掉续播点，恢复不了原状）
        let played = app.buttons["item-played"]
        let position = backend.resume(itemId: itemId)?["position_ms"] as? Int ?? 0
        if played.exists, position == 0 {
            let wasPlayed = played.label == "标记为未看"
            played.tap()
            XCTAssertTrue(played.waitForLabel(wasPlayed ? "标记为已看" : "标记为未看", timeout: 10), "已看状态应随后端返回翻转")
            played.tap()
            XCTAssertTrue(played.waitForLabel(wasPlayed ? "标记为未看" : "标记为已看", timeout: 10), "再点一次应恢复原状")
        }
    }

    /// 条目详情的各区块：媒体轨道、播放键、演职员、文件区（点开看规格）、外部词条
    @MainActor
    func testItemDetailSections() {
        let app = launch(route: "/library")
        _ = openFirstItem(app)
        XCTAssertTrue(app.buttons["item-play"].waitForExistence(timeout: 15), "有在位文件时应有播放键")
        snapshot("详情首屏")
        let files = app.descendants(matching: .any)["item-files"]
        for _ in 0 ..< 8 where !files.isHittable {
            app.swipeUp()
        }
        XCTAssertTrue(files.exists, "应有文件区")
        XCTAssertTrue(app.staticTexts["演职员"].exists || app.staticTexts["相关链接"].exists)
        snapshot("详情下半屏")
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'file-row-'")).firstMatch.tap()
        XCTAssertTrue(app.staticTexts["保存目录"].waitForExistence(timeout: 5), "文件行点开应列出规格")
        snapshot("文件规格")
        Thread.sleep(forTimeInterval: 3)
    }

    /// 管理员 ⋯ 菜单 → 待处理抽屉：四个页签可切换（只看不动）
    @MainActor
    func testPendingDrawerOpens() {
        let app = launch(route: "/library")
        let card = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'library-card-'")).firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 20))
        card.tap()
        let menu = app.buttons["library-actions"]
        XCTAssertTrue(menu.waitForExistence(timeout: 20))
        menu.tap()
        let pending = app.buttons.matching(NSPredicate(format: "label BEGINSWITH '待处理'")).firstMatch
        XCTAssertTrue(pending.waitForExistence(timeout: 5), "管理员菜单应有「待处理」")
        XCTAssertTrue(app.buttons["扫描库"].exists || app.buttons.matching(NSPredicate(format: "label BEGINSWITH '停止扫描'")).firstMatch.exists)
        snapshot("单库 ⋯ 菜单")
        pending.tap()
        XCTAssertTrue(app.navigationBars["待处理"].waitForExistence(timeout: 10), "应打开待处理抽屉")
        Thread.sleep(forTimeInterval: 2)
        snapshot("待处理抽屉")
    }

    /// 加入合集：新建一个测试合集并把当前作品放进去，后端核对后删除
    @MainActor
    func testAddToNewCollection() {
        let app = launch(route: "/library")
        let (_, itemId) = openFirstItem(app)
        let name = "UI测试合集-\(Int(Date().timeIntervalSince1970))"
        app.buttons["item-actions"].tap()
        app.buttons["加入合集…"].tap()
        let field = app.textFields["新建合集，取个名字"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap()
        field.typeText(name)
        app.buttons["新建并加入"].tap()
        var created: [String: Any]?
        XCTAssertTrue(waitUntil {
            created = self.backend.collections().first { $0["name"] as? String == name }
            return created != nil
        }, "后端应出现新合集")
        let collectionId = created?["id"] as? Int ?? 0
        let members = backend.collectionItemIds(collectionId)
        snapshot("加入合集后")
        // 先清理再断言：断言失败也不在服务器上留测试数据
        backend.deleteCollection(collectionId)
        XCTAssertTrue(members.contains(itemId), "新合集里应包含当前作品")
    }

    /// 自定义首页：隐藏「接下来继续」后 400ms 防抖自动保存；测完把偏好整份恢复
    @MainActor
    func testCustomizeHidesRowAndSaves() {
        let original = backend.uiPreferences()
        defer { if let original { backend.saveUiPreferences(original) } }
        let app = launch(route: "/library/customize")
        let eye = app.buttons["row-visibility-up-next"]
        XCTAssertTrue(eye.waitForExistence(timeout: 20))
        eye.tap()
        XCTAssertTrue(waitUntil {
            let rows = (self.backend.uiPreferences()?["home"] as? [String: Any])?["rows"] as? [[String: Any]] ?? []
            return rows.contains { $0["id"] as? String == "up-next" && $0["hidden"] as? Bool == true }
        }, "隐藏应自动保存到后端")
        snapshot("隐藏一行后")
    }

    private func waitUntil(timeout: TimeInterval = 15, _ condition: @escaping () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            Thread.sleep(forTimeInterval: 0.5)
        }
        return false
    }
}

@MainActor
private extension XCUIElement {
    func waitForLabel(_ label: String, timeout: TimeInterval) -> Bool {
        let predicate = NSPredicate(format: "label == %@", label)
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: self)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }
}

/// 测试进程里的最小后端客户端：同步请求 + 会话 Cookie（独立的 URLSession，不影响 App）
private final class Backend {
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
        _ = semaphore.wait(timeout: .now() + 20)
        return result
    }

    func resume(itemId: Int) -> [String: Any]? {
        request("GET", "/playback/resume?media_item_id=\(itemId)&season_number=0&episode_number=0") as? [String: Any]
    }

    func itemCount(libraryId: Int) -> Int {
        let library = request("GET", "/libraries/\(libraryId)") as? [String: Any]
        return (library?["stats"] as? [String: Any])?["item_count"] as? Int ?? 0
    }

    func marks(itemId: Int) -> [String: Any]? {
        request("GET", "/playback/marks?media_item_id=\(itemId)") as? [String: Any]
    }

    func collections() -> [[String: Any]] {
        request("GET", "/collections?include_empty=true") as? [[String: Any]] ?? []
    }

    func collectionItemIds(_ id: Int) -> [Int] {
        (request("GET", "/collections/\(id)/items?limit=200") as? [[String: Any]] ?? []).compactMap { $0["media_item_id"] as? Int }
    }

    func deleteCollection(_ id: Int) {
        request("DELETE", "/collections/\(id)")
    }

    func uiPreferences() -> [String: Any]? {
        request("GET", "/ui/preferences") as? [String: Any]
    }

    func saveUiPreferences(_ prefs: [String: Any]) {
        request("PUT", "/ui/preferences", body: prefs)
    }
}
