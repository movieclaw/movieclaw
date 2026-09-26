import XCTest

/// 订阅模块端到端验收：订阅首页各版块与海报墙、订阅详情（事实区 / 里程碑链 / 排查记录）、
/// 「更多」里的全部对话框（只到预览为止）、订阅弹层 ready / 已订阅两态、规则组编辑器，
/// 以及两项「可逆写操作」：切换自动续订再切回、暂停追踪再恢复。
///
/// 依赖一台真实运行的 MovieClaw，账号通过环境变量传入（scripts/test.sh 已转发）：
/// MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD；未提供密码时整组跳过——仓库开源，不写任何真实密码。
///
/// 安全约束（测试服务器可能是用户的正式订阅，会自动搜种下载）：
/// - 绝不点：确认订阅、立即搜索、开始洗版、保存调整、换组、取消订阅确认、规则组保存；
/// - 只调预览类接口（title-preview / download-routing-preview / removal-preview）；
/// - 可逆写操作只在指定订阅上做并当场恢复：
///   MC_TEST_FOLLOW_SUB（默认 15，剧集，切自动续订）、MC_TEST_PAUSE_SUB（默认 6，追踪中，暂停再恢复）。
final class SubscriptionsUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["MC_TEST_SERVER"] ?? "http://localhost:3000" }
    private var username: String { env["MC_TEST_USERNAME"] ?? "admin" }
    /// 用于浏览详情与打开对话框的订阅（有缺口的剧集）
    private var detailSub: String { env["MC_TEST_SUB_ID"] ?? "3" }
    private var followSub: String { env["MC_TEST_FOLLOW_SUB"] ?? "15" }
    private var pauseSub: String { env["MC_TEST_PAUSE_SUB"] ?? "6" }
    /// 未订阅的作品（ready 态）与已订阅的作品（管理态）
    private var readyRef: String { env["MC_TEST_READY_REF"] ?? "tmdb:tv:1396" }
    private var existingRef: String { env["MC_TEST_EXISTING_REF"] ?? "tmdb:tv:325124" }

    @MainActor
    private func launch(route: String, extra: [String] = []) throws -> XCUIApplication {
        guard let password = env["MC_TEST_PASSWORD"], !password.isEmpty else {
            throw XCTSkip("未提供 MC_TEST_PASSWORD，跳过联调用例")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["-mcServer", server, "-mcUser", username, "-mcPass", password, "-mcRoute", route] + extra
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

    /// 打开详情页「更多」面板里的某一项
    @MainActor
    private func openManage(_ app: XCUIApplication, _ title: String) {
        let more = app.buttons["subscription-more"]
        XCTAssertTrue(more.waitForExistence(timeout: 20))
        more.tap()
        let row = app.buttons["manage-\(title)"]
        XCTAssertTrue(row.waitForExistence(timeout: 10), "管理面板应有「\(title)」")
        row.tap()
    }

    @MainActor
    private func closeTopSheet(_ app: XCUIApplication) {
        let closes = app.buttons.matching(identifier: "sheet-close")
        XCTAssertTrue(closes.firstMatch.waitForExistence(timeout: 10))
        closes.element(boundBy: closes.count - 1).tap()
    }

    // MARK: 首页（流媒体式版式）与海报墙

    /// 首页各版块按数据有无出现：Hero → 刚刚入库 → 日程 → 剧集 / 电影海报行；
    /// 海报行标题「›」压栈到完整海报墙，墙上的海报进订阅详情。
    @MainActor
    func testHomeSectionsAndWall() throws {
        let app = try launch(route: "/subscriptions")
        let cell = app.buttons["subscription-cell"].firstMatch
        XCTAssertTrue(cell.waitForExistence(timeout: 30), "应渲染剧集 / 电影海报行")
        sleep(3) // 等剧照与片名 Logo 加载完再截
        snapshot("订阅首页-01-首屏")
        // Hero 有多张时左滑看后面几张（轮播手动切换后重新计时）
        let hero = app.descendants(matching: .any)["subscriptions-hero"].firstMatch
        if hero.exists {
            for page in 2 ... 4 {
                hero.swipeLeft()
                sleep(2)
                snapshot("订阅首页-01-Hero\(page)")
            }
        }
        for step in 2 ... 4 {
            app.swipeUp(velocity: .slow)
            sleep(2)
            snapshot("订阅首页-0\(step)-下滑")
        }
        // 剧集海报行左滑到底，露出「已收齐」分隔线后面压暗的那几部
        let tvShelf = app.descendants(matching: .any)["section-tv"].firstMatch
        if tvShelf.exists {
            // 在整排容器上滑：滑过的海报会被懒加载回收，按单张海报定位找不到
            for _ in 1 ... 3 {
                tvShelf.swipeLeft()
            }
            sleep(1)
            snapshot("订阅首页-04-剧集行左滑")
            // 超过横滑上限时一直滑到底，末尾是「查看全部」卡
            // 屏幕外的卡判断不了能否点击（isHittable 会直接报错），按坐标看它是否已完整进入屏幕
            let seeAll = app.buttons["shelf-see-all"].firstMatch
            let screen = app.windows.firstMatch.frame
            for _ in 0 ..< 10 where !(seeAll.exists && seeAll.frame.maxX <= screen.maxX && seeAll.frame.minX >= screen.minX) {
                tvShelf.swipeLeft()
            }
            if seeAll.exists {
                sleep(1)
                snapshot("订阅首页-04-剧集行末尾")
            }
        }

        let tvWall = app.buttons["shelf-more-tv"]
        let more = tvWall.exists ? tvWall : app.buttons["shelf-more-movie"]
        XCTAssertTrue(more.waitForExistence(timeout: 10), "海报行标题应能压栈到海报墙")
        more.tap()
        let count = app.staticTexts["subscriptions-count"]
        XCTAssertTrue(count.waitForExistence(timeout: 10), "海报墙应有计数头")
        XCTAssertTrue(count.label.hasPrefix("共 "), "计数头：\(count.label)")
        sleep(2)
        snapshot("订阅首页-05-海报墙")
        // 海报墙分进行中 / 已暂停 / 已收齐三段，往下看后两段
        for step in 1 ... 2 {
            app.swipeUp(velocity: .slow)
            sleep(1)
            snapshot("订阅首页-05-海报墙-下滑\(step)")
        }

        app.buttons["subscription-cell"].firstMatch.tap()
        XCTAssertTrue(app.buttons["subscription-more"].waitForExistence(timeout: 30), "点海报应进入订阅详情")
        snapshot("订阅详情-从海报墙进入")
    }

    // MARK: 详情与对话框（只到预览）

    @MainActor
    func testDetailChainAndDialogsPreviewOnly() throws {
        let app = try launch(route: "/subscriptions/\(detailSub)")
        XCTAssertTrue(app.otherElements["fact-收录范围"].waitForExistence(timeout: 30) || app.staticTexts["收录范围"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.otherElements["progress-strip"].exists || app.staticTexts["收录进度"].exists)

        // 展开一集的里程碑链
        let row = app.buttons["wanted-row"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.tap()
        XCTAssertTrue(app.staticTexts["投递"].waitForExistence(timeout: 5), "展开后应显示里程碑链")
        snapshot("详情-里程碑链")

        // 调整订阅：季结构来自 title-preview（只读），不点保存
        openManage(app, "调整订阅")
        XCTAssertTrue(app.buttons["adjust-save"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["season-1"].waitForExistence(timeout: 20), "调整订阅应列出季")
        XCTAssertFalse(app.buttons["adjust-save"].isEnabled, "未改动时保存键应禁用")
        snapshot("详情-调整订阅")
        closeTopSheet(app)

        // 洗一轮版：只看规则组候选，不点开始
        openManage(app, "洗一轮版")
        XCTAssertTrue(app.buttons["upgrade-run-start"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["upgrade-rule-option"].firstMatch.waitForExistence(timeout: 20), "应列出带洗版目标的规则组")
        snapshot("详情-洗一轮版")
        closeTopSheet(app)

        // 更换规则组：只看列表，不点选
        openManage(app, "更换规则组")
        XCTAssertTrue(app.buttons["ruleset-option"].firstMatch.waitForExistence(timeout: 20))
        snapshot("详情-更换规则组")
        closeTopSheet(app)

        // 取消订阅（管理员）：拉 removal-preview，不点确认
        let more = app.buttons["subscription-more"]
        more.tap()
        let remove = app.buttons["manage-remove"]
        XCTAssertTrue(remove.waitForExistence(timeout: 10))
        remove.tap()
        XCTAssertTrue(app.buttons["confirm-cancel-subscription"].waitForExistence(timeout: 20), "管理员取消订阅应弹带预览的确认层")
        XCTAssertTrue(app.buttons["cancel-delete-torrents"].waitForExistence(timeout: 20))
        snapshot("详情-取消订阅预览")
        closeTopSheet(app)
        XCTAssertTrue(app.buttons["subscription-more"].waitForExistence(timeout: 10), "关闭后仍停留在详情")

        // 排查记录
        let log = app.buttons["activity-log-toggle"]
        if log.waitForExistence(timeout: 5) {
            log.tap()
            snapshot("详情-排查记录")
        }
    }

    // MARK: 订阅弹层

    @MainActor
    func testSubscribeSheetReadyAndRuleSetEditor() throws {
        let app = try launch(route: "/subscriptions", extra: ["-mcSubscribe", readyRef])
        let submit = app.buttons["subscribe-submit"]
        XCTAssertTrue(submit.waitForExistence(timeout: 40), "未订阅作品应进入订阅表单")
        XCTAssertTrue(app.buttons["season-1"].exists, "剧集应列出可勾选的季")
        let ruleset = app.buttons["subscribe-ruleset"]
        XCTAssertTrue(ruleset.exists, "管理员应能选规则组")
        // 条件摘要在规则组行内，读屏标签 = 行名 + 组名 + 摘要
        XCTAssertTrue(ruleset.label.hasPrefix("资源规则") && ruleset.label.count > "资源规则".count + 4, "所选规则组应在行内显示条件摘要：\(ruleset.label)")
        snapshot("订阅弹层-ready")

        // 快捷新建规则组收在「资源规则」菜单末尾：只打开编辑器、展开分段，不保存。
        // 确认订阅在右上角工具栏，与规则组菜单不重叠，不会误点
        app.buttons["subscribe-ruleset"].tap()
        let newRuleset = app.buttons.matching(NSPredicate(format: "identifier == %@ OR label == %@", "subscribe-new-ruleset", "新建规则组…")).firstMatch
        XCTAssertTrue(newRuleset.waitForExistence(timeout: 10), "规则组菜单末尾应有「新建规则组…」")
        newRuleset.tap()
        XCTAssertTrue(app.buttons["ruleset-save"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["ruleset-save"].isEnabled, "未填名称时保存键应禁用")
        app.buttons["ruleset-section-scope"].tap()
        XCTAssertTrue(app.buttons["剧集"].waitForExistence(timeout: 10))
        snapshot("规则组编辑器")
        closeTopSheet(app)
        XCTAssertTrue(submit.waitForExistence(timeout: 10), "关闭编辑器回到订阅表单")
        closeTopSheet(app)
    }

    @MainActor
    func testSubscribeSheetExistingManageState() throws {
        let app = try launch(route: "/subscriptions", extra: ["-mcSubscribe", existingRef])
        XCTAssertTrue(app.staticTexts["subscribe-existing"].waitForExistence(timeout: 40) || app.otherElements["subscribe-existing"].waitForExistence(timeout: 5), "已订阅作品应进入管理态")
        XCTAssertTrue(app.buttons["subscribe-unsubscribe"].exists)
        snapshot("订阅弹层-已订阅")
        closeTopSheet(app)
        XCTAssertTrue(app.buttons["subscription-cell"].firstMatch.waitForExistence(timeout: 10))
    }

    // MARK: 可逆写操作（当场恢复）

    @MainActor
    func testToggleFollowFutureAndRestore() throws {
        let app = try launch(route: "/subscriptions/\(followSub)")
        let fact = app.descendants(matching: .any)["fact-自动续订"].firstMatch
        XCTAssertTrue(fact.waitForExistence(timeout: 30), "剧集订阅应有自动续订事实")
        let wasOn = fact.label.contains("已开启")
        openManage(app, wasOn ? "关闭自动续订" : "开启自动续订")
        XCTAssertTrue(wait(fact, contains: wasOn ? "已关闭" : "已开启"), "切换后应立即反映：\(fact.label)")
        snapshot("自动续订-已切换")
        // 切回原状
        openManage(app, wasOn ? "开启自动续订" : "关闭自动续订")
        XCTAssertTrue(wait(fact, contains: wasOn ? "已开启" : "已关闭"), "应恢复原状：\(fact.label)")
    }

    @MainActor
    func testPauseAndResumeRestore() throws {
        let app = try launch(route: "/subscriptions/\(pauseSub)")
        let status = app.descendants(matching: .any)["subscription-status"].firstMatch
        XCTAssertTrue(status.waitForExistence(timeout: 30))
        XCTAssertTrue(status.label.contains("追踪中"), "测试订阅须为追踪中：\(status.label)")
        openManage(app, "暂停追踪")
        let pause = app.alerts.buttons["暂停追踪"]
        XCTAssertTrue(pause.waitForExistence(timeout: 10), "应先二次确认")
        XCTAssertTrue(app.alerts.buttons["返回"].exists, "取消键应为「返回」（同 Web cancelLabel）")
        pause.tap()
        XCTAssertTrue(wait(status, contains: "已暂停"), "暂停后状态：\(status.label)")
        snapshot("暂停追踪")
        openManage(app, "恢复追踪")
        let resume = app.alerts.buttons["恢复追踪"]
        XCTAssertTrue(resume.waitForExistence(timeout: 10))
        resume.tap()
        XCTAssertTrue(wait(status, contains: "追踪中"), "应恢复追踪：\(status.label)")
    }

    @MainActor
    private func wait(_ element: XCUIElement, contains text: String, timeout: TimeInterval = 20) -> Bool {
        let predicate = NSPredicate(format: "label CONTAINS %@", text)
        return XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: predicate, object: element)], timeout: timeout) == .completed
    }
}
