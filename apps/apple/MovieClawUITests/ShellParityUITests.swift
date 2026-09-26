import XCTest

/// 对等修补三（外壳 · 活动 · 设置 · 跨节）运行期核验：只读打开界面并截图，对照网页手机端。
///
/// 依赖真实服务器（MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD，未提供密码整组跳过——仓库开源，不写真实密码）。
/// 安全约束（测试服务器是用户正在用的正式实例）：
/// - 只打开菜单 / 弹层 / 页签，所有弹层一律点「取消」「关闭」退出，绝不点保存 / 删除 / 确认，全程只读；
/// - 每次点击前断言目标存在、可点且不被底部标签栏遮挡，否则用例失败——绝不按坐标盲点。
final class ShellParityUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["MC_TEST_SERVER"] ?? "http://localhost:3000" }
    private var username: String { env["MC_TEST_USERNAME"] ?? "admin" }
    private var password: String? { env["MC_TEST_PASSWORD"].flatMap { $0.isEmpty ? nil : $0 } }

    private var probe: SettingsTestAPI?

    // MARK: 工具

    @MainActor
    private func launch(route: String) throws -> XCUIApplication {
        guard let password else { throw XCTSkip("未提供 MC_TEST_PASSWORD，跳过联调用例") }
        continueAfterFailure = false
        if probe == nil { probe = try SettingsTestAPI(server: server, username: username, password: password) }
        let app = XCUIApplication()
        app.launchArguments = ["-mcServer", server, "-mcUser", username, "-mcPass", password, "-mcRoute", route]
        app.launch()
        return app
    }

    /// 截图：挂到测试结果里；在模拟器上跑时另存一份到宿主机 /tmp/movieclaw-uitest-shots/（便于与网页截图并排对照）
    @MainActor
    private func snapshot(_ name: String) {
        let screenshot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        #if targetEnvironment(simulator)
        let dir = URL(fileURLWithPath: "/tmp/movieclaw-uitest-shots/ShellParity")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try? screenshot.pngRepresentation.write(to: dir.appending(path: "\(name).png"))
        #endif
    }

    /// 可点且不与标签栏重叠
    @MainActor
    private func tapSafely(_ app: XCUIApplication, _ element: XCUIElement, _ what: String, timeout: TimeInterval = 15) {
        XCTAssertTrue(element.waitForExistence(timeout: timeout), "应出现「\(what)」")
        var swipes = 0
        let tabBar = app.tabBars.firstMatch
        while swipes < 6, element.exists, !element.isHittable || (tabBar.exists && tabBar.isHittable && element.frame.intersects(tabBar.frame)) {
            app.swipeUp(velocity: .slow)
            swipes += 1
        }
        XCTAssertTrue(element.isHittable, "「\(what)」不可点，停止操作")
        if tabBar.exists, tabBar.isHittable {
            XCTAssertFalse(element.frame.intersects(tabBar.frame), "「\(what)」被标签栏遮挡，停止操作")
        }
        element.tap()
    }

    // MARK: 外壳

    /// 「更多」页（标签栏最右的头像页签）：「新会话」入口、会话行长按菜单（三项）、切换账号弹层文案
    @MainActor
    func testMoreTabAndAccountSwitcher() throws {
        let app = try launch(route: "/library")
        let avatarTab = app.tabBars.buttons["open-more"]
        XCTAssertTrue(avatarTab.waitForExistence(timeout: 15), "标签栏最右应有头像页签")
        avatarTab.tap()
        XCTAssertTrue(avatarTab.isSelected, "点头像应切到「更多」页签")
        let row = app.buttons.matching(identifier: "more-session-row").firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 15), "应列出最近会话")
        XCTAssertFalse(app.buttons["会话操作"].exists, "会话行不再挂「⋯」（按 iOS 惯例左滑 / 长按出操作）")
        snapshot("更多-页签")
        XCTAssertTrue(app.buttons["more-new-session"].exists, "最近会话首行应有「新会话」入口")
        XCTAssertTrue(row.isHittable, "会话行不可点，停止操作")
        row.press(forDuration: 1.2)
        for title in ["在新会话中继续", "重命名", "删除会话"] {
            XCTAssertTrue(app.buttons[title].waitForExistence(timeout: 5), "长按菜单应有「\(title)」")
        }
        snapshot("更多-会话菜单")
        // 收起菜单：点菜单里无副作用的地方不存在，改为重启 App 进入下一段
        app.terminate()

        let again = try launch(route: "/library")
        XCTAssertTrue(again.tabBars.buttons["open-more"].waitForExistence(timeout: 15), "标签栏最右应有头像页签")
        again.tabBars.buttons["open-more"].tap()
        tapSafely(again, again.buttons["切换账号"], "切换账号")
        XCTAssertTrue(again.staticTexts["本机已登录的账号，点击即可切换，不用再输密码。"].waitForExistence(timeout: 10))
        XCTAssertTrue(again.staticTexts["当前"].waitForExistence(timeout: 5), "当前账号应标「✓ 当前」")
        snapshot("切换账号")
        tapSafely(again, again.buttons["关闭"], "关闭")
    }

    /// 登录页：「记住我」默认不勾、副标题同 Web（只打开页面，不提交任何登录）。App 登录页是纯黑底，不铺背景图
    @MainActor
    func testLoginPageMatchesWeb() throws {
        guard password != nil else { throw XCTSkip("未提供 MC_TEST_PASSWORD，跳过联调用例") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--reset-state", "--ui-testing"]
        app.launch()
        let field = app.textFields["server-address"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        tapSafely(app, field, "服务器地址")
        field.typeText(server)
        tapSafely(app, app.buttons["connect-button"], "连接")
        XCTAssertTrue(app.textFields["login-username"].waitForExistence(timeout: 15), "连接成功后应进入登录页")
        XCTAssertTrue(app.staticTexts["使用你的 MovieClaw 账号进入。"].exists)
        let remember = app.switches["30 天内记住我"]
        XCTAssertTrue(remember.exists)
        XCTAssertEqual(remember.value as? String, "0", "「记住我」默认不勾（同 Web）")
        snapshot("登录页")
    }

    /// /my 压栈打开：只有返回，没有「完成」（R-7）
    @MainActor
    func testMyRouteHasSingleExit() throws {
        let app = try launch(route: "/my")
        XCTAssertTrue(app.staticTexts["最近会话"].waitForExistence(timeout: 20))
        XCTAssertFalse(app.buttons["完成"].exists, "压栈打开的「更多」不应再叠「完成」")
        snapshot("我的-压栈")
    }

    // MARK: 设置深链（只读打开，弹层一律取消）

    @MainActor
    func testSettingsDeepLinks() throws {
        _ = try launch(route: "/settings")
        let downloaders = try probe?.getArray("/downloaders") ?? []
        XCUIApplication().terminate()

        if let first = downloaders.first, let id = first["id"] as? Int {
            // 拥堵提示「去调整」：直达限速与队列
            let app = try launch(route: "/settings/downloaders?limits=\(id)")
            XCTAssertTrue(app.navigationBars.staticTexts.matching(NSPredicate(format: "label BEGINSWITH '限速与队列'")).firstMatch
                .waitForExistence(timeout: 20), "应自动打开限速与队列")
            snapshot("深链-限速与队列")
            tapSafely(app, app.buttons["sheet-close"], "取消")
            app.terminate()

            // 体检修复卡「去补映射」：进默认下载器编辑并预填映射（不保存）
            let app2 = try launch(route: "/settings/downloaders?suggest_mapping=%2Fios-test-suggest")
            XCTAssertTrue(app2.navigationBars["编辑下载器"].waitForExistence(timeout: 20), "应自动进默认下载器的编辑")
            let note = app2.staticTexts["downloader-form-suggest-note"]
            var swipes = 0
            while !note.exists, swipes < 6 {
                app2.swipeUp(velocity: .slow)
                swipes += 1
            }
            XCTAssertTrue(note.exists, "应显示预填提示")
            // 映射行的本机侧（目录选择按钮 / 文本框）显示预填的路径；提示文案里也含这段路径，所以至少两处
            let mentions = app2.descendants(matching: .any)
                .matching(NSPredicate(format: "label CONTAINS '/ios-test-suggest' OR value CONTAINS '/ios-test-suggest'"))
            swipes = 0
            while mentions.count < 2, swipes < 8 { // 预填行追加在已有映射之后，往下翻着找
                app2.swipeUp(velocity: .slow)
                swipes += 1
            }
            XCTAssertGreaterThanOrEqual(mentions.count, 2, "应预填一行映射的本机侧")
            snapshot("深链-补映射-预填行")
            snapshot("深链-补映射")
            tapSafely(app2, app2.buttons["sheet-close"], "取消")
            app2.terminate()
        }

        // 体检修复卡「去建规则」：打开新建规则并预选自动路由（不保存）
        let app3 = try launch(route: "/settings/import-watch?suggest=auto&kinds=movie,tv")
        XCTAssertTrue(app3.navigationBars["添加自动入库规则"].waitForExistence(timeout: 20), "应自动打开新建规则")
        snapshot("深链-建规则")
        tapSafely(app3, app3.buttons["sheet-close"], "取消")
        // 取消即放弃队列：不应再弹第二条
        XCTAssertFalse(app3.navigationBars["添加自动入库规则"].waitForExistence(timeout: 3), "取消后不应继续预填下一类型")
        app3.terminate()

        let app4 = try launch(route: "/settings/sites?tab=search")
        let picker = app4.segmentedControls["sites-tab"]
        XCTAssertTrue(picker.waitForExistence(timeout: 20))
        XCTAssertTrue(picker.buttons["搜索分类"].isSelected, "?tab=search 应直达搜索分类")
        snapshot("深链-搜索分类")
        app4.terminate()

        let app5 = try launch(route: "/settings/app?tab=storage")
        XCTAssertTrue(app5.buttons["app-tab-缓存管理"].waitForExistence(timeout: 20))
        XCTAssertTrue(app5.buttons["app-tab-缓存管理"].isSelected, "?tab=storage 应直达缓存管理")
        snapshot("深链-缓存管理")
        app5.terminate()

        let app6 = try launch(route: "/settings/im-push?tab=content")
        let push = app6.segmentedControls["push-tab"]
        XCTAssertTrue(push.waitForExistence(timeout: 20))
        XCTAssertTrue(push.buttons["推送内容"].isSelected, "?tab=content 应直达推送内容")
        snapshot("深链-推送内容")
    }
}
