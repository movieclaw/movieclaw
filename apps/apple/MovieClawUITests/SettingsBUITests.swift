import XCTest

/// 设置（下）九个分区的端到端验收：订阅规则、资源站点、下载器、自动入库、刮削与整理、
/// 消息推送、Webhook、模型接入、MCP 服务。
///
/// 依赖一台真实运行的 MovieClaw，账号通过环境变量传入（scripts/test.sh 已转发）：
/// MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD；未提供密码时整组跳过——仓库开源，不写任何真实密码。
///
/// 安全铁律（测试服务器是用户正式在用的 NAS）：
/// - 每次点击前都经 `tap(_:_:)`：断言目标存在、`isHittable`、且不与标签栏重叠，否则用例直接失败——
///   XCUITest 按坐标点击，被遮挡时会点到别的按钮（曾有模块因此误建真实订阅）；
/// - 真实配置（站点、下载器、入库规则、供应商、推送、刮削、MCP 等）只浏览、只打开表单，绝不点保存 / 确认 / 删除；
/// - 写操作只针对本用例自己创建的数据，并在同一用例内删除：
///   `ios-test-` 前缀的规则组（新建 → 编辑 → 删除）、`ios-test-` 前缀且**停用**的 Webhook 端点（URL 指向 example.invalid）；
/// - 允许的只读 / 无副作用操作：模拟一单（只调预览接口）、下载器「测试连接」。
final class SettingsBUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["MC_TEST_SERVER"] ?? "http://localhost:3000" }
    private var username: String { env["MC_TEST_USERNAME"] ?? "admin" }
    private var app: XCUIApplication!

    @MainActor
    private func launch(route: String) throws {
        guard let password = env["MC_TEST_PASSWORD"], !password.isEmpty else {
            throw XCTSkip("未提供 MC_TEST_PASSWORD，跳过联调用例")
        }
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchArguments = ["-mcServer", server, "-mcUser", username, "-mcPass", password, "-mcRoute", route]
        app.launch()
    }

    @MainActor
    private func snapshot(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    // MARK: 安全点击

    /// 目标是否完整可见且不被标签栏盖住
    @MainActor
    private func clear(_ element: XCUIElement) -> Bool {
        guard element.exists, element.isHittable else { return false }
        // 标签栏只在露出时才算遮挡（sheet 盖住时它仍 exists，但不可点）
        let tabBar = app.tabBars.firstMatch
        if tabBar.exists, tabBar.isHittable, tabBar.frame.intersects(element.frame) { return false }
        return app.windows.firstMatch.frame.contains(element.frame)
    }

    /// 把目标滚进可点区域（最多滑 8 次）
    @MainActor
    private func reveal(_ element: XCUIElement, _ what: String, timeout: TimeInterval = 20) {
        // Form/List 懒加载：屏幕外的行还不存在，先滑动找
        var scrolls = 0
        while !element.waitForExistence(timeout: scrolls == 0 ? timeout : 2), scrolls < 8 {
            app.swipeUp(velocity: .slow)
            scrolls += 1
        }
        XCTAssertTrue(element.exists, "\(what) 应存在")
        var tries = 0
        while !clear(element), tries < 8 {
            app.swipeUp(velocity: .slow)
            tries += 1
        }
        XCTAssertTrue(clear(element), "\(what) 不可点击或被遮挡，为避免误点停止用例")
    }

    /// 唯一的点击入口：存在、可点、未遮挡才点
    @MainActor
    private func tap(_ element: XCUIElement, _ what: String, timeout: TimeInterval = 20) {
        reveal(element, what, timeout: timeout)
        element.tap()
    }

    @MainActor
    private func type(_ element: XCUIElement, _ what: String, text: String, clearing: Int = 0) {
        tap(element, what)
        if clearing > 0 {
            element.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: clearing))
        }
        element.typeText(text)
    }

    @MainActor
    private func waitGone(_ element: XCUIElement, timeout: TimeInterval = 20) -> Bool {
        let predicate = NSPredicate(format: "exists == false")
        return XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: predicate, object: element)], timeout: timeout) == .completed
    }

    private func testName() -> String {
        "ios-test-\(Int(Date().timeIntervalSince1970) % 100_000)"
    }

    // MARK: 订阅规则

    /// 模拟一单：只调搜索与预览接口
    @MainActor
    func testSimulateOrderPreviewOnly() throws {
        try launch(route: "/settings/subscription")
        let field = app.textFields["simulate-query"]
        type(field, "模拟一单输入框", text: "葬送的芙莉莲")
        let candidate = app.buttons["simulate-candidate"].firstMatch
        tap(candidate, "候选条目", timeout: 30)
        let ok = app.descendants(matching: .any)["simulate-ok"]
        let warning = app.descendants(matching: .any)["simulate-warning"]
        let deadline = Date().addingTimeInterval(30)
        while !ok.exists && !warning.exists && Date() < deadline { usleep(500_000) }
        XCTAssertTrue(ok.exists || warning.exists, "应给出预演结论")
        XCTAssertTrue(app.descendants(matching: .any)["simulate-step"].firstMatch.exists, "应列出预演步骤")
        snapshot("订阅规则-模拟一单")
    }

    /// 规则组：新建 ios-test- 组 → 改名 → 删除（只动自己建的组；不设默认，不设适用范围）
    @MainActor
    func testRuleSetCreateEditDelete() throws {
        try launch(route: "/settings/subscription")
        let name = testName()
        let renamed = name + "-b"

        tap(app.buttons["ruleset-create"], "新建规则组")
        type(app.textFields["ruleset-name"], "规则组名称", text: name)
        tap(app.buttons["ruleset-save"], "规则组保存")
        let row = app.buttons["ruleset-name-\(name)"]
        reveal(row, "新建的规则组行", timeout: 30)
        snapshot("订阅规则-新建后")

        // 编辑：点组名打开编辑器，改名保存
        tap(row, "规则组名（编辑）")
        let nameField = app.textFields["ruleset-name"]
        XCTAssertTrue(nameField.waitForExistence(timeout: 20))
        XCTAssertEqual(nameField.value as? String, name, "编辑器应预填当前组名")
        type(nameField, "规则组名称", text: renamed, clearing: name.count + 4)
        tap(app.buttons["ruleset-save"], "规则组保存")
        let renamedRow = app.buttons["ruleset-name-\(renamed)"]
        reveal(renamedRow, "改名后的规则组行", timeout: 30)

        // 删除：菜单 → 删除 → 二次确认（确认框只针对自己建的组）
        tap(app.buttons["ruleset-menu-\(renamed)"], "规则组操作菜单")
        tap(app.buttons["删除"], "菜单删除项")
        let confirm = app.alerts.buttons["删除"]
        XCTAssertTrue(app.alerts.staticTexts["删除规则组「\(renamed)」？"].waitForExistence(timeout: 10), "确认框必须指向测试组")
        tap(confirm, "删除确认")
        XCTAssertTrue(waitGone(renamedRow), "删除后测试组应从列表消失")
        snapshot("订阅规则-删除后")
    }

    // MARK: Webhook

    /// 新建停用的 ios-test- 端点 → 改名 → 删除；总开关不动
    @MainActor
    func testWebhookEndpointCreateEditDelete() throws {
        try launch(route: "/settings/webhook")
        let name = testName()
        let renamed = name + "-b"
        let globalToggle = app.switches["webhook-global-toggle"]
        XCTAssertTrue(globalToggle.waitForExistence(timeout: 30))
        let globalBefore = globalToggle.value as? String

        tap(app.buttons["webhook-create"], "新增 Endpoint")
        type(app.textFields["webhook-name"], "显示名", text: name)
        type(app.textFields["webhook-url"], "目标地址", text: "https://example.invalid/hook")
        // 新建默认启用：先关掉，确保测试端点不会收到任何事件
        let enabled = app.switches["webhook-draft-enabled"]
        reveal(enabled, "启用开关")
        if (enabled.value as? String) == "1" {
            let knob = enabled.switches.firstMatch
            tap(knob.exists ? knob : enabled, "启用开关")
        }
        XCTAssertEqual(enabled.value as? String, "0", "测试端点必须是停用状态")
        snapshot("Webhook-新建表单")
        tap(app.buttons["webhook-save"], "保存")

        // 自有协议新建后一次性展示密钥
        tap(app.buttons["webhook-secret-dismiss"], "我已保存，关闭", timeout: 30)
        let edit = app.buttons["webhook-edit-\(name)"]
        reveal(edit, "测试端点编辑按钮", timeout: 20)
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label CONTAINS '已停用'")).firstMatch.exists, "测试端点应显示已停用")
        snapshot("Webhook-新建后")

        // 编辑：改名
        tap(edit, "编辑")
        type(app.textFields["webhook-name"], "显示名", text: renamed, clearing: name.count + 4)
        tap(app.buttons["webhook-save"], "保存")
        let renamedEdit = app.buttons["webhook-edit-\(renamed)"]
        reveal(renamedEdit, "改名后的编辑按钮", timeout: 20)

        // 删除
        tap(renamedEdit, "编辑")
        tap(app.buttons["webhook-delete"], "删除")
        XCTAssertTrue(app.alerts.staticTexts["删除「\(renamed)」？"].waitForExistence(timeout: 10), "确认框必须指向测试端点")
        tap(app.alerts.buttons["删除"], "删除确认")
        XCTAssertTrue(waitGone(renamedEdit), "删除后测试端点应消失")
        XCTAssertEqual(globalToggle.value as? String, globalBefore, "总开关不应被改动")
        snapshot("Webhook-删除后")
    }


    // MARK: 只读浏览（真实配置只看、只开表单，不点任何保存 / 确认 / 删除）

    @MainActor
    func testSitesBrowseAndAddFormOnly() throws {
        try launch(route: "/settings/sites")
        let row = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'site-row-'")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 30), "应列出已接入站点")
        tap(row, "站点行（展开详情）")
        XCTAssertEqual(row.value as? String, "已展开")
        snapshot("资源站点-展开详情")
        tap(app.buttons["site-add"], "添加站点")
        XCTAssertTrue(app.textFields["site-add-search"].waitForExistence(timeout: 20) || app.searchFields.firstMatch.waitForExistence(timeout: 5), "添加站点弹层应有站点搜索")
        snapshot("资源站点-添加站点表单")
        tap(app.buttons["sheet-close"], "关闭弹层")
        tap(app.segmentedControls["sites-tab"].buttons["搜索分类"], "搜索分类页签")
        snapshot("资源站点-搜索分类")
    }

    /// 下载器：展开详情并点「测试连接」（允许的写操作：只触发连接自检）
    @MainActor
    func testDownloaderVerifyConnection() throws {
        try launch(route: "/settings/downloaders")
        let row = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'downloader-row-'")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 30), "应列出下载器")
        let name = String(row.identifier.dropFirst("downloader-row-".count))
        tap(row, "下载器行（展开详情）")
        let verify = app.buttons["downloader-verify-\(name)"]
        tap(verify, "重新测试连接")
        // 测试中按钮禁用、文案变「测试中…」，2s 轮询后恢复
        let done = XCTNSPredicateExpectation(predicate: NSPredicate(format: "isEnabled == true"), object: verify)
        XCTAssertEqual(XCTWaiter().wait(for: [done], timeout: 40), .completed, "测试连接应在 40 秒内出结果")
        snapshot("下载器-测试连接后")
    }

    @MainActor
    func testImportWatchScrapePushBrowse() throws {
        try launch(route: "/settings/import-watch")
        XCTAssertTrue(app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'import-watch-rule-'")).firstMatch.waitForExistence(timeout: 30)
            || app.buttons["import-watch-add"].waitForExistence(timeout: 5), "自动入库应渲染规则或空态")
        snapshot("自动入库")

        try launch(route: "/settings/scrape")
        let save = app.buttons["scrape-save"]
        reveal(save, "刮削保存键（页底，只看不点）", timeout: 30)
        XCTAssertFalse(save.isEnabled, "未改动时保存键应禁用")
        snapshot("刮削与整理")

        try launch(route: "/settings/im-push")
        XCTAssertTrue(app.descendants(matching: .any)["push-channels-count"].waitForExistence(timeout: 30)
            || app.descendants(matching: .any)["push-channels-empty"].exists, "应显示已接入账号数或空态")
        tap(app.segmentedControls["push-tab"].buttons["推送内容"], "推送内容页签")
        XCTAssertTrue(app.buttons["push-test"].waitForExistence(timeout: 20), "推送内容页应有测试推送（不点）")
        snapshot("消息推送-推送内容")
    }

    @MainActor
    func testLLMAndMCPBrowse() throws {
        try launch(route: "/settings/llm")
        XCTAssertTrue(app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'llm-provider-'")).firstMatch.waitForExistence(timeout: 30), "应列出已接入的供应商")
        tap(app.buttons["llm-create"].firstMatch, "接入另一家供应商")
        tap(app.buttons["llm-form-cancel"], "取消（不保存）")
        snapshot("模型接入")

        try launch(route: "/settings/mcp")
        XCTAssertTrue(app.switches["mcp-enabled"].waitForExistence(timeout: 30), "应有 MCP 总开关（不点）")
        snapshot("MCP 服务")
    }
}
