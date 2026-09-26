import XCTest

/// 媒体库管理页与访客分享页的端到端验收。
///
/// 依赖一台真实运行的 MovieClaw，账号通过环境变量传入（scripts/test.sh 已转发）：
/// MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD；未提供密码时整组跳过——仓库开源，不写任何真实密码。
/// 分享用例另需一条**专为测试新建**的分享：TEST_RUNNER_MC_TEST_SHARE_SLUG / TEST_RUNNER_MC_TEST_SHARE_PASSWORD
/// （xcodebuild 会把 TEST_RUNNER_ 前缀的环境变量转给测试进程）；没给就跳过。
///
/// 安全铁律（测试服务器是用户正式在用的 NAS）：
/// - 每次点击都经 `tap(_:_:)`：断言目标存在、`isHittable`、且不与标签栏重叠，否则用例直接失败——
///   XCUITest 按坐标点击，被遮挡时会点到别的按钮（曾有模块因此误建真实订阅）；
/// - 只做「允许且可恢复」的写操作：调整顺序（改后在同一用例里改回）、首页显示开关（切换后切回）、
///   取消**本用例专用的测试分享**；
/// - 新建 / 编辑 / 删除库、设默认、扫描、整理、刷新、生成章节、封面、回收站与重复文件的处理一律不点，
///   编辑表单只打开看、点「取消」关闭。
final class ManageUITests: XCTestCase {
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
        let tabBar = app.tabBars.firstMatch
        if tabBar.exists, tabBar.isHittable, tabBar.frame.intersects(element.frame) { return false }
        return app.windows.firstMatch.frame.contains(element.frame)
    }

    /// 等目标出现并滚进可点区域（最多滑 6 次）
    @MainActor
    private func reveal(_ element: XCUIElement, _ what: String, timeout: TimeInterval = 20) {
        // Form / 懒加载列表：屏幕外的行还不存在，先滑动找
        var scrolls = 0
        while !element.waitForExistence(timeout: scrolls == 0 ? timeout : 2), scrolls < 6 {
            app.swipeUp(velocity: .slow)
            scrolls += 1
        }
        XCTAssertTrue(element.exists, "\(what) 应存在")
        var tries = 0
        while !clear(element), tries < 6 {
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
    private func any(_ identifier: String) -> XCUIElement {
        app.descendants(matching: .any)[identifier].firstMatch
    }

    /// 第一个媒体库行的 id（按屏幕顺序）
    @MainActor
    private func libraryRowIds() -> [Int] {
        let rows = app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'manage-row-menu-'"))
        return rows.allElementsBoundByIndex
            .sorted { $0.frame.minY < $1.frame.minY }
            .compactMap { Int($0.identifier.replacingOccurrences(of: "manage-row-menu-", with: "")) }
    }

    // MARK: 用例

    /// 四个页签逐个打开：库列表、回收站、重复文件、分享都能渲染（只读）
    @MainActor
    func testTabsBrowse() throws {
        try launch(route: "/library/manage")
        XCTAssertTrue(any("manage-summary").waitForExistence(timeout: 30), "页头摘要应出现")
        XCTAssertTrue(app.textFields["manage-search"].waitForExistence(timeout: 20), "搜索框应出现")
        XCTAssertFalse(libraryRowIds().isEmpty, "应至少有一个媒体库行")
        snapshot("管理页-媒体库")

        // 搜索：输入一个不可能命中的词 → 空结果提示 → 清除筛选
        tap(app.textFields["manage-search"], "搜索框")
        app.textFields["manage-search"].typeText("不存在的库名zzz")
        XCTAssertTrue(app.staticTexts["没有符合条件的媒体库"].waitForExistence(timeout: 5), "应提示没有符合条件的媒体库")
        tap(app.buttons["manage-clear-filter"], "清除筛选")
        XCTAssertFalse(libraryRowIds().isEmpty, "清除筛选后应恢复列表")

        tap(app.buttons["manage-tab-recycle"], "回收站页签")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS '个文件' OR label CONTAINS '回收站'")).firstMatch.waitForExistence(timeout: 30), "回收站应有摘要或空态")
        snapshot("管理页-回收站")

        tap(app.buttons["manage-tab-duplicates"], "重复文件页签")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS '扫描' OR label CONTAINS '重复'")).firstMatch.waitForExistence(timeout: 30), "重复文件应有扫描信息或空态")
        snapshot("管理页-重复文件")

        tap(app.buttons["manage-tab-shares"], "分享页签")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS '分享' OR label CONTAINS '打开'")).firstMatch.waitForExistence(timeout: 30), "分享页签应有列表或空态")
        snapshot("管理页-分享")
    }

    /// 编辑库表单：只打开、展开分区看内容，点「取消」关闭（不保存）
    @MainActor
    func testEditFormOpenOnly() throws {
        try launch(route: "/library/manage")
        XCTAssertTrue(any("manage-summary").waitForExistence(timeout: 30))
        guard let first = libraryRowIds().first else { return XCTFail("没有媒体库行") }
        tap(app.buttons["manage-row-menu-\(first)"], "行菜单")
        tap(app.buttons["编辑库"], "编辑库菜单项")
        XCTAssertTrue(app.buttons["form-section-basic"].waitForExistence(timeout: 20), "编辑表单应打开")
        snapshot("编辑库-首屏")
        tap(app.buttons["form-section-scan"], "扫描与监控分区")
        XCTAssertTrue(app.switches["form-switch-watch"].waitForExistence(timeout: 5), "应出现实时监控开关")
        tap(app.buttons["form-section-access"], "可见范围分区")
        XCTAssertTrue(any("form-access-mode").waitForExistence(timeout: 5), "应出现可见范围选择")
        snapshot("编辑库-可见范围")
        tap(app.buttons["form-cancel"], "取消")
        XCTAssertTrue(app.buttons["form-section-basic"].waitForNonExistence(timeout: 10), "表单应关闭")
    }

    /// 新建向导：只走到第 2 步看表单，点「上一步」「取消」退出（不创建）
    @MainActor
    func testCreateWizardOpenOnly() throws {
        try launch(route: "/library/manage")
        tap(app.buttons["manage-create"], "创建媒体库")
        tap(app.buttons["form-kind-movie"], "电影类型卡")
        XCTAssertTrue(app.textFields["form-name"].waitForExistence(timeout: 5), "应进入名称与目录")
        XCTAssertEqual(app.textFields["form-name"].value as? String, "电影库", "名称应按类型预填")
        XCTAssertFalse(app.buttons["form-primary"].isEnabled, "没有根目录时不能下一步")
        snapshot("新建向导-名称与目录")
        tap(app.buttons["form-back"], "上一步")
        tap(app.buttons["form-cancel"], "取消")
        XCTAssertTrue(app.buttons["form-kind-movie"].waitForNonExistence(timeout: 10), "向导应关闭")
    }

    /// 调整顺序：把第一个库下移一位保存，验证顺序变了，再改回并验证恢复
    @MainActor
    func testReorderAndRestore() throws {
        try launch(route: "/library/manage")
        XCTAssertTrue(any("manage-summary").waitForExistence(timeout: 30))
        _ = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'manage-row-menu-'")).firstMatch.waitForExistence(timeout: 20)
        let original = libraryRowIds()
        guard original.count >= 2 else { throw XCTSkip("只有一个库，无法验证排序") }

        // 下移第一个
        tap(app.buttons["manage-row-menu-\(original[0])"], "行菜单")
        tap(app.buttons["调整顺序"], "调整顺序菜单项")
        tap(app.buttons["reorder-down-\(original[0])"], "第一个库下移")
        tap(app.buttons["reorder-save"], "保存顺序")
        var swapped = original
        swapped.swapAt(0, 1)
        let changed = NSPredicate { _, _ in self.libraryRowIds() == swapped }
        XCTAssertEqual(XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: changed, object: nil)], timeout: 15), .completed,
                       "顺序应变为 \(swapped)")
        snapshot("调整顺序-已改")

        // 改回
        tap(app.buttons["manage-row-menu-\(original[0])"], "行菜单")
        tap(app.buttons["调整顺序"], "调整顺序菜单项")
        tap(app.buttons["reorder-up-\(original[0])"], "上移回原位")
        tap(app.buttons["reorder-save"], "保存顺序")
        let restored = NSPredicate { _, _ in self.libraryRowIds() == original }
        XCTAssertEqual(XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: restored, object: nil)], timeout: 15), .completed,
                       "顺序应恢复为 \(original)")
    }

    /// 首页显示开关：切换后切回，两次都验证行内备注
    @MainActor
    func testHomeToggleAndRestore() throws {
        try launch(route: "/library/manage")
        XCTAssertTrue(any("manage-summary").waitForExistence(timeout: 30))
        _ = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'manage-row-menu-'")).firstMatch.waitForExistence(timeout: 20)
        guard let id = libraryRowIds().first else { return XCTFail("没有媒体库行") }
        let row = any("manage-row-\(id)")
        let excludedNote = row.staticTexts.matching(NSPredicate(format: "label CONTAINS '从首页排除'")).firstMatch
        let wasExcluded = excludedNote.exists

        tap(app.buttons["manage-row-menu-\(id)"], "行菜单")
        tap(app.buttons[wasExcluded ? "在首页展示" : "从首页排除"], "首页显示开关")
        XCTAssertTrue(wasExcluded ? excludedNote.waitForNonExistence(timeout: 15) : excludedNote.waitForExistence(timeout: 15), "行内备注应随开关变化")

        tap(app.buttons["manage-row-menu-\(id)"], "行菜单")
        tap(app.buttons[wasExcluded ? "从首页排除" : "在首页展示"], "首页显示开关（恢复）")
        XCTAssertTrue(wasExcluded ? excludedNote.waitForExistence(timeout: 15) : excludedNote.waitForNonExistence(timeout: 15), "应恢复原状态")
    }

    /// 访客分享页：密码卡 → 解锁 → 详情 → 播放 → 关闭；最后在管理页「分享」页签取消这条测试分享
    @MainActor
    func testShareGuestPageThenRevoke() throws {
        guard let slug = env["MC_TEST_SHARE_SLUG"], !slug.isEmpty,
              let sharePassword = env["MC_TEST_SHARE_PASSWORD"], !sharePassword.isEmpty else {
            throw XCTSkip("未提供测试分享 MC_TEST_SHARE_SLUG / MC_TEST_SHARE_PASSWORD，跳过")
        }
        try launch(route: "/s/\(slug)")
        let gate = any("share-gate")
        let title = any("share-item-title")
        // 本机上次解锁过则 Cookie 仍在，直接进详情
        let deadline = Date().addingTimeInterval(30)
        while !gate.exists, !title.exists, Date() < deadline { usleep(300_000) }
        if gate.exists {
            XCTAssertFalse(title.exists, "解锁前不应露出片名")
            snapshot("分享-密码卡")
            // 先验一次错误密码的提示（只错一次，不会触发锁定）
            let field = app.secureTextFields["share-password"]
            tap(field, "密码输入框")
            field.typeText("wrong-pass")
            tap(app.buttons["share-unlock"], "打开")
            XCTAssertTrue(any("share-password-error").waitForExistence(timeout: 15), "错误密码应有提示")
            tap(field, "密码输入框")
            field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 12) + sharePassword)
            tap(app.buttons["share-unlock"], "打开")
        }
        XCTAssertTrue(title.waitForExistence(timeout: 30), "解锁后应显示影片页")
        XCTAssertTrue(any("share-expiry").exists, "应显示到期提示")
        snapshot("分享-详情")

        tap(app.buttons["share-play"], "播放")
        XCTAssertTrue(any("player-screen").waitForExistence(timeout: 30), "播放器没有打开")
        sleep(4)
        snapshot("分享-播放")
        // 关闭播放器：起播失败时错误遮罩盖住控制层，用遮罩上的「返回」；否则用控制条的关闭键
        // （横屏时第一下是退出横屏，再点一次才关闭；控制条自动隐藏时先点画面唤出）
        let close = app.buttons["player-close"]
        let failedBack = app.buttons["返回"]
        for _ in 0 ..< 4 where any("player-screen").exists {
            if failedBack.exists, failedBack.isHittable {
                failedBack.tap()
            } else {
                if !(close.exists && close.isHittable) {
                    app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.7)).tap()
                    _ = close.waitForExistence(timeout: 2)
                }
                if close.exists, close.isHittable { close.tap() }
            }
            _ = any("player-screen").waitForNonExistence(timeout: 3)
        }
        XCTAssertTrue(any("player-screen").waitForNonExistence(timeout: 10), "播放器没有关闭")

        // 取消这条测试分享（只针对传入的 slug）
        app.terminate()
        try launch(route: "/library/manage?tab=shares")
        let revoke = app.buttons["share-revoke-\(slug)"]
        tap(revoke, "测试分享的「取消」", timeout: 30)
        let confirm = app.alerts.buttons["取消分享"]
        tap(confirm, "确认取消分享", timeout: 5)
        XCTAssertTrue(any("share-row-\(slug)").waitForNonExistence(timeout: 20), "测试分享应从列表消失")

        // 取消后访客页应整页提示失效
        app.terminate()
        try launch(route: "/s/\(slug)")
        XCTAssertTrue(any("share-unavailable").waitForExistence(timeout: 30), "取消后的分享应提示不可用")
        snapshot("分享-已取消")
    }
}
