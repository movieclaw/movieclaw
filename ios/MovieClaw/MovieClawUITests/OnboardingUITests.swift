import XCTest

/// 首次启动流程的端到端验收：输入地址 → 测试连接 → 登录 → 进入主界面。
///
/// 依赖一台真实运行的 MovieClaw（默认本机 dev 环境 http://localhost:3000）。
/// 通过环境变量覆盖（xcodebuild 需加 TEST_RUNNER_ 前缀传入）：
///   MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD
final class OnboardingUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["MC_TEST_SERVER"] ?? "http://localhost:3000" }
    private var username: String { env["MC_TEST_USERNAME"] ?? "admin" }
    private var password: String { env["MC_TEST_PASSWORD"] ?? "mclaw-dev-2026" }

    @MainActor
    private func launchFresh() -> XCUIApplication {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--reset-state", "--ui-testing"]
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

    @MainActor
    func testUnreachableServerShowsError() {
        let app = launchFresh()
        let field = app.textFields["server-address"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap()
        field.typeText("127.0.0.1:1")
        app.buttons["connect-button"].tap()
        let error = app.staticTexts.containing(NSPredicate(format: "label CONTAINS '无法连接'")).firstMatch
        XCTAssertTrue(error.waitForExistence(timeout: 15))
        snapshot("连接失败")
    }

    @MainActor
    func testConnectAndLogin() {
        let app = launchFresh()
        let field = app.textFields["server-address"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap()
        // 故意带上路径，验证「粘贴浏览器地址栏」也能用
        field.typeText("\(server)/login")
        snapshot("输入地址")
        app.buttons["connect-button"].tap()

        let user = app.textFields["login-username"]
        XCTAssertTrue(user.waitForExistence(timeout: 15), "连接成功后应进入登录页")
        user.tap()
        user.typeText(username)
        let pass = app.secureTextFields["login-password"]
        pass.tap()
        pass.typeText("wrong-password")
        app.buttons["login-submit"].tap()
        XCTAssertTrue(app.staticTexts["login-error"].waitForExistence(timeout: 10), "密码错误应提示")
        snapshot("密码错误")

        pass.tap()
        pass.clearSecure()
        pass.typeText(password)
        app.buttons["login-submit"].tap()

        XCTAssertTrue(app.tabBars.firstMatch.waitForExistence(timeout: 15), "登录后应进入主界面")
        snapshot("登录成功")

        // 冷启动后应保持登录（Cookie 持久化）
        app.terminate()
        app.launchArguments = ["--ui-testing"]
        app.launch()
        XCTAssertTrue(app.tabBars.firstMatch.waitForExistence(timeout: 15), "重启后应仍处于登录状态")
    }
}

@MainActor
private extension XCUIElement {
    /// SecureField 没法读回内容，按足够多次退格清空
    func clearSecure() {
        typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 40))
    }
}
