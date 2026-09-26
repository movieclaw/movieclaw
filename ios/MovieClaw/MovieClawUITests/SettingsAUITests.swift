import XCTest

/// 设置模块（上）端到端验收：概览、个人信息、外观、成员、设备、播放、AI 设定、更新与维护、网络、系统日志。
///
/// 依赖一台真实运行的 MovieClaw，账号经环境变量传入（scripts/test.sh 已转发）：
/// MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD；未提供密码时整组跳过——仓库开源，不写任何真实密码。
///
/// 安全约束（测试服务器是用户正在用的正式实例）：
/// - 绝不点：修改密码、清空观看记录的确认、应用更新 / 回退 / 重启的确认、代理与外部访问 / 端口、保留版本数、
///   清理缓存、定时任务、别人的设备令牌与接入请求、已有成员的任何写操作、远程转码保存；
/// - 可逆写操作动手前经接口记下原值，`tearDown` 一律经接口按原值写回（界面改回之外的第二道保险）：
///   昵称、主题 / 质感 / 导航顺序（整份 ui.preferences）、播放策略两颗开关、AI 默认模型；
/// - 自建数据只用 `ios-test-` 前缀（成员、CLI 令牌），`tearDown` 兜底清理同前缀的残留；
/// - 每次点击前断言目标存在、可点且不被底部标签栏遮挡（`tapSafely`），否则用例失败——绝不按坐标盲点。
final class SettingsAUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["MC_TEST_SERVER"] ?? "http://localhost:3000" }
    private var username: String { env["MC_TEST_USERNAME"] ?? "admin" }
    private var password: String? { env["MC_TEST_PASSWORD"].flatMap { $0.isEmpty ? nil : $0 } }

    private var probe: SettingsTestAPI?
    /// 用例开始时记下的原值（tearDown 写回）
    private var restorers: [(String, () throws -> Void)] = []

    override func tearDownWithError() throws {
        for (name, restore) in restorers.reversed() {
            do { try restore() } catch { XCTFail("恢复「\(name)」失败：\(error)") }
        }
        restorers = []
        // 兜底清理自建数据（只动 ios-test- 前缀）
        if let probe {
            for member in (try? probe.getArray("/members")) ?? [] {
                if let name = member["username"] as? String, name.hasPrefix("ios-test-"), let id = member["id"] as? Int {
                    _ = try? probe.request("DELETE", "/members/\(id)")
                }
            }
            for token in (try? probe.getArray("/auth/tokens")) ?? [] {
                if let name = token["name"] as? String, name.hasPrefix("ios-test-"), let id = token["id"] as? String {
                    _ = try? probe.request("DELETE", "/auth/tokens/\(id)")
                }
            }
        }
    }

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

    /// 记下某个接口的原始 data，tearDown 时用 PUT 原样写回
    private func rememberPut(_ name: String, get path: String, put putPath: String? = nil, transform: (([String: Any]) -> [String: Any])? = nil) throws {
        guard let probe else { return }
        let original = try probe.getObject(path)
        let body = transform?(original) ?? original
        restorers.append((name, { _ = try probe.request("PUT", putPath ?? path, body: body) }))
    }

    @MainActor
    private func snapshot(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    /// 把元素滚进可视区（避开底部标签栏）后返回。列表是懒加载的：还没出现的行先往下翻着找
    @MainActor
    private func reveal(_ app: XCUIApplication, _ element: XCUIElement, timeout: TimeInterval = 20, maxSwipes: Int = 10) -> XCUIElement {
        if !element.waitForExistence(timeout: timeout) {
            // 先往回翻到顶（元素可能在当前视口上方），再往下找
            var tries = 0
            while !element.exists, tries < 4 {
                app.swipeDown(velocity: .fast)
                tries += 1
            }
            tries = 0
            while !element.exists, tries < maxSwipes {
                app.swipeUp(velocity: .slow)
                tries += 1
            }
        }
        XCTAssertTrue(element.exists, "应出现：\(element)")
        var swipes = 0
        while swipes < maxSwipes, element.exists, !isUnobscured(app, element) {
            let keyboard = app.keyboards.firstMatch
            let tabTop = app.tabBars.firstMatch.exists ? app.tabBars.firstMatch.frame.minY : app.frame.maxY
            if keyboard.exists, element.frame.intersects(keyboard.frame) {
                // 键盘挡住了：往下拖一下列表收起键盘（列表设置了滚动即收键盘）
                app.swipeDown(velocity: .slow)
            } else if element.frame.maxY > tabTop - 8 || element.frame.minY > app.frame.maxY {
                app.swipeUp(velocity: .slow)
            } else {
                app.swipeDown(velocity: .slow)
            }
            swipes += 1
        }
        return element
    }

    /// 元素可点，且不与底部标签栏、键盘重叠
    @MainActor
    private func isUnobscured(_ app: XCUIApplication, _ element: XCUIElement) -> Bool {
        guard element.exists, element.isHittable else { return false }
        let tabBar = app.tabBars.firstMatch
        if tabBar.exists, tabBar.isHittable, element.frame.intersects(tabBar.frame) { return false }
        let keyboard = app.keyboards.firstMatch
        if keyboard.exists, element.frame.intersects(keyboard.frame) { return false }
        return true
    }

    /// 安全点击：必须存在、可点、不被遮挡，否则用例失败（绝不按坐标盲点）
    @MainActor
    private func tapSafely(_ app: XCUIApplication, _ element: XCUIElement, _ what: String) {
        let target = reveal(app, element)
        guard isUnobscured(app, target) else {
            XCTFail("「\(what)」不可点或被遮挡，停止操作")
            return
        }
        target.tap()
    }

    /// 切换分区内的胶囊页签（纯界面状态）：列表刚刷新时偶尔吞掉一次点按，没切过去就再点
    @MainActor
    private func selectTab(_ app: XCUIApplication, _ id: String) {
        let tab = app.buttons[id]
        for _ in 0 ..< 3 {
            tapSafely(app, tab, id)
            if tab.waitForSelected(timeout: 3) { return }
        }
        XCTFail("页签「\(id)」没有切换过去")
    }

    /// 拨动开关：优先点开关本体（整行元素的中心落在标题上，点了不一定拨动）
    @MainActor
    private func toggleSafely(_ app: XCUIApplication, _ id: String) {
        let row = reveal(app, app.switches[id])
        let inner = row.switches.firstMatch
        tapSafely(app, inner.exists ? inner : row, id)
    }

    /// 弹出的确认框：标题必须含 expected（确认点的是自己创建的数据），再点指定按钮
    @MainActor
    private func confirmAlert(_ app: XCUIApplication, titleContains expected: String, button: String) {
        let alert = app.alerts.firstMatch
        XCTAssertTrue(alert.waitForExistence(timeout: 10), "应弹出确认框")
        XCTAssertTrue(alert.label.contains(expected), "确认框标题「\(alert.label)」应含「\(expected)」，拒绝确认")
        guard alert.label.contains(expected) else { return }
        let target = alert.buttons[button]
        XCTAssertTrue(target.exists && target.isHittable, "确认框应有「\(button)」")
        target.tap()
    }

    @MainActor
    private func cancelAlert(_ app: XCUIApplication) {
        let alert = app.alerts.firstMatch
        XCTAssertTrue(alert.waitForExistence(timeout: 10), "应弹出确认框")
        alert.buttons["取消"].tap()
        XCTAssertTrue(alert.waitForNonExistence(timeout: 5))
    }

    // MARK: 概览

    @MainActor
    func testOverviewPipelineHealth() throws {
        let app = try launch(route: "/settings/overview")
        XCTAssertTrue(app.staticTexts["订阅链路体检"].waitForExistence(timeout: 30))
        let recheck = app.buttons["overview-recheck"]
        tapSafely(app, recheck, "重新体检")
        let ready = app.staticTexts["公共链路"].waitForExistence(timeout: 30)
            || app.otherElements["setup-checklist"].waitForExistence(timeout: 5)
        XCTAssertTrue(ready, "体检结果应渲染开局清单或公共链路")
        snapshot("概览-链路体检")
    }

    // MARK: 个人信息

    @MainActor
    func testProfileNicknameRoundTripAndGuards() throws {
        let app = try launch(route: "/settings/profile")
        guard let probe else { return }
        let original = try probe.getObject("/auth/me")["nickname"] as? String ?? username
        restorers.append(("昵称", { _ = try probe.request("PUT", "/auth/profile", body: ["nickname": original]) }))

        // 改昵称 → 校验 → 改回
        let temp = "ios-test-\(Int(Date().timeIntervalSince1970) % 100000)"
        tapSafely(app, app.buttons["profile-nickname-edit"], "编辑昵称")
        let field = app.textFields["profile-nickname-field"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        field.clearAndType(temp)
        tapSafely(app, app.buttons["profile-nickname-save"], "保存昵称")
        XCTAssertTrue(waitUntil(15) { (try? probe.getObject("/auth/me"))?["nickname"] as? String == temp }, "接口昵称应更新为 \(temp)")
        XCTAssertTrue(app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", temp)).firstMatch.waitForExistence(timeout: 10), "界面应显示新昵称")
        snapshot("个人信息-改昵称")

        tapSafely(app, app.buttons["profile-nickname-edit"], "编辑昵称")
        field.tap()
        field.clearAndType(original)
        tapSafely(app, app.buttons["profile-nickname-save"], "保存昵称")
        XCTAssertTrue(waitUntil(15) { (try? probe.getObject("/auth/me"))?["nickname"] as? String == original }, "接口昵称应改回 \(original)")

        // 修改密码：未填全时按钮禁用（不提交）
        let change = reveal(app, app.buttons["profile-change-password"])
        XCTAssertFalse(change.isEnabled, "未填全三项时修改密码应禁用")

        // 清空全部观看记录：只到确认框，点取消
        tapSafely(app, app.buttons["profile-clear-history"], "清空观看记录")
        XCTAssertTrue(app.alerts.firstMatch.waitForExistence(timeout: 10) && app.alerts.firstMatch.label.contains("清空全部观看记录"))
        cancelAlert(app)
        snapshot("个人信息-清空记录已取消")
    }

    // MARK: 外观

    @MainActor
    func testAppearanceThemeTextureNavRestore() throws {
        let app = try launch(route: "/settings/appearance")
        guard let probe else { return }
        try rememberPut("界面偏好", get: "/ui/preferences")

        // 主题：App 固定银玻璃，Netflix 卡片置灰不可选；App 不做背景图设定，没有「背景图」页签
        XCTAssertTrue(app.buttons["theme-silver"].waitForExistence(timeout: 20))
        XCTAssertFalse(app.buttons["theme-netflix"].isEnabled, "Netflix 卡片应置灰")
        XCTAssertFalse(app.buttons["appearance-tab-背景图"].exists, "App 不应有背景图页签")
        snapshot("外观-主题")

        // 界面质感：拖侧栏透明度 → 保存 → 接口可见变化；网页的蒙版参数原样带回（tearDown 写回原值）
        let before = try probe.getObject("/ui/preferences")
        let originalScrim = before["scrim"] as? [String: Any]
        let originalTransparency = (before["sidebar"] as? [String: Any])?["transparency"] as? Double ?? 0
        selectTab(app, "appearance-tab-界面质感")
        XCTAssertFalse(app.sliders["slider-蒙版暗度"].exists, "App 不应有蒙版滑杆")
        let slider = reveal(app, app.sliders["slider-侧栏透明度"])
        slider.adjust(toNormalizedSliderPosition: originalTransparency > 0.5 ? 0.15 : 0.85)
        let save = reveal(app, app.buttons["texture-save"])
        XCTAssertTrue(save.isEnabled, "拖动滑杆后保存键应可用")
        tapSafely(app, save, "保存质感")
        XCTAssertTrue(waitUntil(15) {
            let value = ((try? probe.getObject("/ui/preferences"))?["sidebar"] as? [String: Any])?["transparency"] as? Double
            return value.map { abs($0 - originalTransparency) > 0.1 } ?? false
        }, "侧栏透明度应已保存")
        let scrimAfter = (try probe.getObject("/ui/preferences"))["scrim"] as? [String: Any]
        XCTAssertEqual(scrimAfter?["blur"] as? Double, originalScrim?["blur"] as? Double, "蒙版模糊度应原样保留")
        XCTAssertEqual(scrimAfter?["dark"] as? Double, originalScrim?["dark"] as? Double, "蒙版暗度应原样保留")
        snapshot("外观-界面质感")

        // 导航顺序：媒体库下移一格 → 保存 → 恢复默认
        selectTab(app, "appearance-tab-导航顺序")
        tapSafely(app, app.buttons["nav-down-library"], "媒体库下移")
        tapSafely(app, app.buttons["nav-save"], "保存导航顺序")
        XCTAssertTrue(waitUntil(15) {
            (((try? probe.getObject("/ui/preferences"))?["nav"] as? [String: Any])?["order"] as? [String])?.firstIndex(of: "library") ?? 0 > 1
        }, "导航顺序应已保存")
        snapshot("外观-导航顺序")
    }

    // MARK: 成员

    @MainActor
    func testMemberLifecycleOnTestAccount() throws {
        let app = try launch(route: "/settings/members")
        guard let probe else { return }
        let name = "ios-test-\(Int(Date().timeIntervalSince1970) % 1_000_000)"

        tapSafely(app, app.buttons["member-add"], "添加成员")
        let usernameField = app.textFields["member-create-username"]
        XCTAssertTrue(usernameField.waitForExistence(timeout: 10))
        usernameField.tap()
        usernameField.typeText(name)
        let nicknameField = app.textFields["member-create-nickname"]
        nicknameField.tap()
        nicknameField.typeText(name)
        tapSafely(app, app.buttons["member-create-regenerate"], "重新生成密码")
        tapSafely(app, app.buttons["member-create-submit"], "创建成员")
        XCTAssertTrue(app.buttons["credential-密码"].waitForExistence(timeout: 20), "应显示一次性密码")
        snapshot("成员-已创建")
        tapSafely(app, app.buttons["password-result-done"], "完成")

        let member = try XCTUnwrap(try probe.getArray("/members").first { $0["username"] as? String == name }, "接口应有新成员")
        let memberId = try XCTUnwrap(member["id"] as? Int)

        // 编辑：打开站点搜索 → 保存
        tapSafely(app, app.buttons["member-menu-\(name)"], "成员菜单")
        tapSafely(app, app.buttons["编辑成员"], "编辑成员")
        toggleSafely(app, "member-allow-search")
        tapSafely(app, app.buttons["member-edit-save"], "保存成员")
        XCTAssertTrue(waitUntil(15) {
            (try? probe.getArray("/members"))?.first { $0["id"] as? Int == memberId }?["allow_search"] as? Bool == true
        }, "站点搜索应已打开")

        // 停用 → 启用
        tapSafely(app, app.buttons["member-menu-\(name)"], "成员菜单")
        tapSafely(app, app.buttons["停用成员"], "停用成员")
        confirmAlert(app, titleContains: name, button: "停用成员")
        XCTAssertTrue(waitUntil(15) {
            (try? probe.getArray("/members"))?.first { $0["id"] as? Int == memberId }?["status"] as? String != "active"
        }, "应已停用")
        tapSafely(app, app.buttons["member-menu-\(name)"], "成员菜单")
        tapSafely(app, app.buttons["启用成员"], "启用成员")
        XCTAssertTrue(waitUntil(15) {
            (try? probe.getArray("/members"))?.first { $0["id"] as? Int == memberId }?["status"] as? String == "active"
        }, "应已启用")
        snapshot("成员-停用启用")

        // 删除
        tapSafely(app, app.buttons["member-menu-\(name)"], "成员菜单")
        tapSafely(app, app.buttons["删除成员"], "删除成员")
        confirmAlert(app, titleContains: name, button: "删除成员")
        XCTAssertTrue(waitUntil(15) {
            !((try? probe.getArray("/members")) ?? []).contains { $0["id"] as? Int == memberId }
        }, "应已删除")
    }

    // MARK: 设备

    @MainActor
    func testCreateAndRevokeOwnToken() throws {
        let app = try launch(route: "/settings/devices")
        guard let probe else { return }
        let name = "ios-test-token-\(Int(Date().timeIntervalSince1970) % 1_000_000)"
        XCTAssertTrue(app.staticTexts["已连接的设备"].waitForExistence(timeout: 20))

        tapSafely(app, app.buttons["token-create-open"], "创建令牌入口")
        let field = app.textFields["token-name"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        field.typeText(name)
        tapSafely(app, app.buttons["token-create-submit"], "创建令牌")
        XCTAssertTrue(app.buttons["token-created-dismiss"].waitForExistence(timeout: 20), "应显示一次性令牌卡")
        XCTAssertTrue(app.staticTexts["已创建「\(name)」"].exists)
        snapshot("设备-令牌已创建")
        tapSafely(app, app.buttons["token-created-dismiss"], "我已保存")
        confirmAlert(app, titleContains: "关闭后就看不到这枚令牌了", button: "我已保存")

        let revoke = app.buttons["device-revoke-\(name)"]
        tapSafely(app, revoke, "吊销自建令牌")
        confirmAlert(app, titleContains: name, button: "吊销")
        XCTAssertTrue(waitUntil(15) {
            !((try? probe.getArray("/auth/tokens")) ?? []).contains { $0["name"] as? String == name }
        }, "自建令牌应已吊销")
        snapshot("设备-已吊销")
    }

    // MARK: 播放

    @MainActor
    func testPlaybackPolicyTogglesRestore() throws {
        let app = try launch(route: "/settings/playback")
        guard let probe else { return }
        let policy = try probe.getObject("/playback/policy")
        let trick = policy["trickplay_enabled"] as? Bool ?? true
        let cache = policy["transcode_cache_enabled"] as? Bool ?? true
        restorers.append(("播放策略", {
            _ = try probe.request("PUT", "/playback/policy", body: ["trickplay_enabled": trick, "transcode_cache_enabled": cache])
        }))

        for (id, key, original) in [("playback-trickplay", "trickplay_enabled", trick), ("playback-transcode-cache", "transcode_cache_enabled", cache)] {
            toggleSafely(app, id)
            XCTAssertTrue(waitUntil(15) { (try? probe.getObject("/playback/policy"))?[key] as? Bool == !original }, "\(key) 应已切换")
            toggleSafely(app, id)
            XCTAssertTrue(waitUntil(15) { (try? probe.getObject("/playback/policy"))?[key] as? Bool == original }, "\(key) 应已恢复")
        }

        // 播放引擎（本机偏好）：切到 MPV 再切回自动
        let engine = app.segmentedControls["playback-engine"]
        tapSafely(app, engine.buttons["MPV"], "MPV")
        XCTAssertTrue(engine.buttons["MPV"].isSelected)
        tapSafely(app, engine.buttons["自动"], "自动")

        // 远程转码：只看，不保存
        XCTAssertTrue(reveal(app, app.switches["remote-transcode-enabled"]).exists)
        snapshot("播放-远程转码")
    }

    // MARK: AI 设定

    @MainActor
    func testAIDefaultModelSwitchRestore() throws {
        let app = try launch(route: "/settings/ai")
        guard let probe else { return }
        let defaults = try probe.getObject("/llm/defaults")
        let agent = defaults["agent_model"] as? String
        let subtitle = defaults["subtitle_model"] as? String
        restorers.append(("AI 默认模型", {
            _ = try probe.request("PUT", "/llm/defaults", body: ["agent_model": agent.map { $0 as Any } ?? NSNull(), "subtitle_model": subtitle.map { $0 as Any } ?? NSNull()])
        }))
        let models = try probe.getArray("/llm/models")
        guard let other = models.first(where: { $0["ref"] as? String != agent }), let otherRef = other["ref"] as? String,
              let otherLabel = other["label"] as? String, let agent,
              let originalLabel = models.first(where: { $0["ref"] as? String == agent })?["label"] as? String else {
            throw XCTSkip("模型清单不足两个，跳过切换")
        }

        func pick(_ label: String) {
            tapSafely(app, app.buttons["ai-agent-model"], "智能体默认模型")
            let option = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", label)).firstMatch
            tapSafely(app, option, "模型 \(label)")
        }
        pick(otherLabel)
        XCTAssertTrue(waitUntil(15) { (try? probe.getObject("/llm/defaults"))?["agent_model"] as? String == otherRef }, "应切到 \(otherRef)")
        snapshot("AI 设定-已切换")
        pick(originalLabel)
        XCTAssertTrue(waitUntil(15) { (try? probe.getObject("/llm/defaults"))?["agent_model"] as? String == agent }, "应切回 \(agent)")
    }

    // MARK: 更新与维护（只读 + 确认框只到取消）

    @MainActor
    func testMaintenanceTabsReadOnly() throws {
        let app = try launch(route: "/settings/app")
        XCTAssertTrue(app.staticTexts["app-current-version"].waitForExistence(timeout: 30), "应显示当前版本")
        snapshot("更新与维护-版本")
        // 重启应用：只到确认框，点取消
        tapSafely(app, app.buttons["app-restart"], "重启应用")
        XCTAssertTrue(app.alerts.firstMatch.waitForExistence(timeout: 10) && app.alerts.firstMatch.label.contains("重启应用"))
        cancelAlert(app)
        XCTAssertFalse(app.otherElements["restart-waiting"].exists, "取消后不应进入重启等待")

        selectTab(app, "app-tab-缓存管理")
        XCTAssertTrue(app.staticTexts["磁盘概览"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["storage-refresh"].exists, "应有刷新统计按钮")
        snapshot("更新与维护-缓存")

        selectTab(app, "app-tab-定时任务")
        XCTAssertTrue(app.staticTexts["后台任务各自按周期运行；改动立即生效，不用重启。周期与启停按任务记在服务器上。"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.segmentedControls.firstMatch.waitForExistence(timeout: 20), "应列出任务与周期方式")
        snapshot("更新与维护-定时任务")
    }

    // MARK: 网络（只点「测试」）

    @MainActor
    func testNetworkServiceTestOnly() throws {
        let app = try launch(route: "/settings/network")
        XCTAssertTrue(app.segmentedControls["network-proxy-mode"].waitForExistence(timeout: 20))
        tapSafely(app, app.buttons["network-test-tmdb"], "TMDB 测试")
        XCTAssertTrue(app.otherElements["network-test-result-tmdb"].waitForExistence(timeout: 40)
            || app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH '连通' OR label == '不通'")).firstMatch.waitForExistence(timeout: 5),
            "应显示测试结果")
        snapshot("网络-测试结果")
    }

    // MARK: 系统日志

    @MainActor
    func testLogsFilterSearchRefreshFullscreen() throws {
        let app = try launch(route: "/settings/logs")
        XCTAssertTrue(app.staticTexts["logs-meta"].waitForExistence(timeout: 30), "应显示日志大小与行数")
        tapSafely(app, app.buttons["logs-level-警告"], "警告筛选")
        tapSafely(app, app.buttons["logs-level-全部"], "全部筛选")
        let search = app.textFields["logs-search"]
        tapSafely(app, search, "搜索框")
        search.typeText("GET")
        snapshot("日志-搜索")
        let refresh = app.segmentedControls["logs-auto-refresh"]
        tapSafely(app, refresh.buttons["30s"], "30s")
        tapSafely(app, refresh.buttons["10s"], "10s")
        tapSafely(app, app.buttons["logs-fullscreen"], "全屏")
        XCTAssertTrue(app.buttons["logs-fullscreen-done"].waitForExistence(timeout: 10))
        snapshot("日志-全屏")
        app.buttons["logs-fullscreen-done"].tap()
    }

    private func waitUntil(_ timeout: TimeInterval, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            Thread.sleep(forTimeInterval: 0.8)
        }
        return condition()
    }
}

// MARK: - 接口探针（测试进程自己的会话，只用于记原值、核对与写回）

final class SettingsTestAPI {
    private let base: URL
    private let session: URLSession
    /// 登录拿到的会话 Cookie（自己带，不与 App / 其它用例共享 Cookie 存储）
    private var cookies: [HTTPCookie] = []

    init(server: String, username: String, password: String) throws {
        base = URL(string: server)!.appending(path: "api/v1")
        let config = URLSessionConfiguration.ephemeral
        config.httpShouldSetCookies = false
        config.httpCookieStorage = nil
        session = URLSession(configuration: config)
        _ = try request("POST", "/auth/login", body: ["username": username, "password": password, "remember": false])
    }

    @discardableResult
    func request(_ method: String, _ path: String, body: [String: Any]? = nil) throws -> Any? {
        var request = URLRequest(url: base.appending(path: String(path.dropFirst())))
        request.httpMethod = method
        if !cookies.isEmpty {
            request.setValue(cookies.map { "\($0.name)=\($0.value)" }.joined(separator: "; "), forHTTPHeaderField: "Cookie")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let semaphore = DispatchSemaphore(value: 0)
        var result: (Data?, URLResponse?, Error?)
        session.dataTask(with: request) { data, response, error in
            result = (data, response, error)
            semaphore.signal()
        }.resume()
        semaphore.wait()
        if let error = result.2 { throw error }
        let http = result.1 as? HTTPURLResponse
        let status = http?.statusCode ?? 0
        if let http, let url = http.url, let fields = http.allHeaderFields as? [String: String] {
            let fresh = HTTPCookie.cookies(withResponseHeaderFields: fields, for: url)
            for cookie in fresh {
                cookies.removeAll { $0.name == cookie.name }
                cookies.append(cookie)
            }
        }
        let json = result.0.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
        guard (200 ..< 300).contains(status) else {
            throw NSError(domain: "SettingsTestAPI", code: status, userInfo: [NSLocalizedDescriptionKey: "\(method) \(path) → \(status) \(json?["message"] ?? "")"])
        }
        return json?["data"]
    }

    func getObject(_ path: String) throws -> [String: Any] {
        try request("GET", path) as? [String: Any] ?? [:]
    }

    func getArray(_ path: String) throws -> [[String: Any]] {
        try request("GET", path) as? [[String: Any]] ?? []
    }
}

private extension XCUIElement {
    func clearAndType(_ text: String) {
        // 光标放到末尾再删（输入框右对齐，点中心会把光标落在文字前面）
        coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.5)).tap()
        if let current = value as? String, !current.isEmpty, current != placeholderValue {
            typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: current.count + 2))
        }
        typeText(text)
    }

    func waitForSelected(timeout: TimeInterval) -> Bool {
        let predicate = NSPredicate(format: "isSelected == true")
        return XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: predicate, object: self)], timeout: timeout) == .completed
    }
}
