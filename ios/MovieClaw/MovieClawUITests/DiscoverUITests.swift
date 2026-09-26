import XCTest

/// 发现与搜索模块的端到端验收：发现页切换类型/数据源、组合筛选、详情页、影人页、
/// 搜索面板、影视/媒体库搜索、站点资源结果的排序/视图/筛选、资源操作面板与下载目标对话框。
///
/// 依赖一台真实运行的 MovieClaw，账号通过环境变量传入（xcodebuild 需加 TEST_RUNNER_ 前缀，
/// scripts/test.sh 已转发）：MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD。
/// 未提供密码时整组跳过——仓库开源，不在代码里写任何真实密码。
///
/// 安全约束：
/// - 不发起新的站点资源实时搜索（会打真实 PT 站）：站点资源用例从「最近搜索」打开**已有快照**
///   （MC_TEST_TORRENT_KEYWORD，默认「沙丘」，需先在网页或 App 里搜过一次）；
/// - 下载目标对话框只验证到预览（resolve-target）为止，**不点「确认下载」**；
/// - 不订阅、不改院线地区。
final class DiscoverUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var server: String { env["MC_TEST_SERVER"] ?? "http://localhost:3000" }
    private var username: String { env["MC_TEST_USERNAME"] ?? "admin" }
    private var torrentKeyword: String { env["MC_TEST_TORRENT_KEYWORD"] ?? "沙丘" }

    @MainActor
    private func launch(route: String? = nil) throws -> XCUIApplication {
        guard let password = env["MC_TEST_PASSWORD"], !password.isEmpty else {
            throw XCTSkip("未提供 MC_TEST_PASSWORD，跳过联调用例")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["-mcServer", server, "-mcUser", username, "-mcPass", password]
        if let route { app.launchArguments += ["-mcRoute", route] }
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

    // MARK: 发现页

    @MainActor
    func testDiscoverSwitchTypeSourceAndOpenDetail() throws {
        let app = try launch()
        XCTAssertTrue(app.otherElements["discover-hero"].waitForExistence(timeout: 30), "TMDB 电影视角应有 Hero 轮播")
        let firstCard = app.buttons["poster-card"].firstMatch
        XCTAssertTrue(firstCard.waitForExistence(timeout: 30), "应渲染海报行")
        snapshot("发现-电影-TMDB")

        // 电影 → 剧集
        app.segmentedControls["discover-type"].buttons["剧集"].tap()
        XCTAssertTrue(app.staticTexts["今日精选 · 剧集"].waitForExistence(timeout: 30) || app.buttons["poster-card"].waitForExistence(timeout: 30))
        snapshot("发现-剧集-TMDB")

        // TMDB → 豆瓣：筛选键仅 TMDB 显示
        app.buttons["discover-source"].tap()
        app.buttons["豆瓣"].tap()
        XCTAssertTrue(app.buttons["poster-card"].waitForExistence(timeout: 30), "豆瓣视角应有海报行")
        XCTAssertFalse(app.buttons["discover-filter"].exists, "豆瓣视角不支持筛选")
        snapshot("发现-剧集-豆瓣")

        // 点海报：有订阅权限时首点只展开信息层，再点才进详情（同 Web 触屏）
        let card = app.buttons["poster-card"].firstMatch
        XCTAssertTrue(card.isHittable)
        card.tap()
        XCTAssertTrue(app.buttons["poster-card-subscribe"].waitForExistence(timeout: 5), "首点应展开信息层（含订阅键）")
        snapshot("海报信息层")
        XCTAssertFalse(app.scrollViews["media-detail"].exists, "首点不应直接进详情")
        card.tap()
        XCTAssertTrue(app.scrollViews["media-detail"].waitForExistence(timeout: 30), "再点海报应进入影片详情")
        snapshot("详情-豆瓣")
    }

    /// 切电影/剧集要清空筛选（两套类型 ID），回到该类型的发现首页（同 Web switchMediaType）
    @MainActor
    func testSwitchTypeClearsFilters() throws {
        let app = try launch()
        let filter = app.buttons["discover-filter"]
        XCTAssertTrue(filter.waitForExistence(timeout: 30))
        filter.tap()
        let genre = app.buttons["动作"]
        XCTAssertTrue(genre.waitForExistence(timeout: 20))
        genre.tap()
        app.buttons["filter-apply"].tap()
        XCTAssertTrue(app.staticTexts["筛选结果"].waitForExistence(timeout: 20))
        let tv = app.segmentedControls["discover-type"].buttons["剧集"]
        XCTAssertTrue(tv.isHittable)
        tv.tap()
        XCTAssertTrue(app.otherElements["discover-hero"].waitForExistence(timeout: 30), "切到剧集应回到发现首页")
        XCTAssertFalse(app.staticTexts["筛选结果"].exists, "筛选应已清空")
        XCTAssertTrue(app.buttons["筛选影片"].exists, "筛选键不应再带角标")
        snapshot("切类型后")
    }

    /// 已订阅的条目点「已订阅」：打开订阅弹层的管理态（同 Web openSubscribe），不跳订阅详情
    @MainActor
    func testSubscribedButtonOpensManageSheet() throws {
        let titleRoute = env["MC_TEST_SUBSCRIBED_MEDIA"] ?? "/media/movie/1003596"
        let app = try launch(route: titleRoute)
        let subscribed = app.buttons["detail-subscribed"]
        guard subscribed.waitForExistence(timeout: 30) else {
            throw XCTSkip("\(titleRoute) 不是已订阅条目，跳过")
        }
        XCTAssertTrue(subscribed.isHittable)
        subscribed.tap()
        XCTAssertTrue(app.staticTexts["subscribe-existing"].waitForExistence(timeout: 20) || app.otherElements["subscribe-existing"].exists, "应进入订阅弹层管理态")
        snapshot("订阅管理态")
        // 只点「好的」关闭，绝不点「取消订阅」
        let ok = app.buttons["subscribe-ok"]
        XCTAssertTrue(ok.isHittable)
        ok.tap()
        XCTAssertFalse(app.buttons["subscribe-ok"].waitForExistence(timeout: 3))
        XCTAssertFalse(app.otherElements["subscription-detail"].exists, "不应跳到订阅详情")
    }

    @MainActor
    func testDiscoverCombinedFilter() throws {
        let app = try launch()
        let filter = app.buttons["discover-filter"]
        XCTAssertTrue(filter.waitForExistence(timeout: 30))
        filter.tap()
        let apply = app.buttons["filter-apply"]
        XCTAssertTrue(apply.waitForExistence(timeout: 10))
        // 选一个类型（等类型清单加载）
        let genre = app.buttons["剧情"]
        XCTAssertTrue(genre.waitForExistence(timeout: 20))
        genre.tap()
        snapshot("组合发现")
        apply.tap()
        XCTAssertTrue(app.staticTexts["筛选结果"].waitForExistence(timeout: 20), "应进入筛选结果网格")
        XCTAssertTrue(app.buttons["poster-card"].waitForExistence(timeout: 30), "筛选结果应有海报")
        XCTAssertTrue(app.buttons["筛选，已启用 1 项"].exists)
        snapshot("筛选结果")
        app.buttons["filtered-clear"].tap()
        XCTAssertTrue(app.otherElements["discover-hero"].waitForExistence(timeout: 30), "清除筛选回到发现页")
    }

    // MARK: 详情与影人

    @MainActor
    func testMediaDetailSectionsAndPerson() throws {
        let app = try launch(route: "/media/movie/550")
        XCTAssertTrue(app.scrollViews["media-detail"].waitForExistence(timeout: 30))
        XCTAssertTrue(app.staticTexts["搏击俱乐部"].exists)
        snapshot("详情-头部")
        let toggle = app.buttons["detail-overview-toggle"]
        if toggle.exists {
            toggle.tap()
            XCTAssertTrue(app.buttons["收起"].waitForExistence(timeout: 5) || toggle.label.contains("收起"))
        }
        let cast = app.otherElements["detail-cast"]
        let scroll = app.scrollViews["media-detail"]
        for _ in 0 ..< 4 where !cast.exists { scroll.swipeUp() }
        XCTAssertTrue(cast.waitForExistence(timeout: 5), "应有演职员行")
        snapshot("详情-演职员")
        // 剧照与海报：打开灯箱再关闭（App 没有背景图设定，灯箱不带「设为背景」）
        let photos = app.otherElements["detail-photos"]
        for _ in 0 ..< 4 where !photos.exists { scroll.swipeUp() }
        if photos.exists {
            photos.buttons.element(boundBy: photos.buttons.count > 2 ? 2 : 0).tap()
            XCTAssertTrue(app.buttons["lightbox-close"].waitForExistence(timeout: 10), "应打开剧照灯箱")
            XCTAssertFalse(app.buttons["设为背景"].exists, "App 不提供「设为背景」")
            XCTAssertTrue(app.scrollViews["lightbox-thumbnails"].exists || app.otherElements["lightbox-thumbnails"].exists, "灯箱底部应有缩略图条")
            snapshot("剧照灯箱")
            app.buttons["lightbox-close"].tap()
        }
        // 演职员 → TMDB 影人页
        for _ in 0 ..< 6 where !cast.isHittable { scroll.swipeDown() }
        cast.buttons.firstMatch.tap()
        XCTAssertTrue(app.staticTexts["TMDB 影人"].waitForExistence(timeout: 30), "应进入 TMDB 影人页")
        XCTAssertTrue(app.buttons["poster-card"].waitForExistence(timeout: 10))
        snapshot("TMDB影人")
    }

    // MARK: 搜索

    @MainActor
    func testSearchHomeAndMediaLibraryVerticals() throws {
        let app = try launch()
        let searchButton = app.navigationBars.buttons["open-search"]
        XCTAssertTrue(searchButton.waitForExistence(timeout: 20), "标签根页右上角应有搜索")
        searchButton.tap()
        XCTAssertTrue(app.otherElements["search-home"].waitForExistence(timeout: 10) || app.scrollViews["search-home"].waitForExistence(timeout: 10))
        let mode = app.segmentedControls["search-mode"]
        XCTAssertTrue(mode.waitForExistence(timeout: 10))
        mode.buttons["影视"].tap()
        snapshot("搜索面板")
        let field = app.searchFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap()
        field.typeText("流浪地球\n")
        XCTAssertTrue(app.scrollViews["media-results"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["poster-card"].waitForExistence(timeout: 40), "影视搜索应有结果")
        snapshot("影视搜索结果")
        app.segmentedControls["search-vertical"].buttons["媒体库"].tap()
        let group = app.otherElements["library-group"].firstMatch
        let empty = app.otherElements["library-empty"]
        XCTAssertTrue(group.waitForExistence(timeout: 20) || empty.exists, "媒体库垂直应出分组或空态")
        snapshot("媒体库搜索结果")
    }

    @MainActor
    func testTorrentSnapshotSortViewFilterAndDownloadDialog() throws {
        let app = try launch()
        let searchButton = app.navigationBars.buttons["open-search"]
        XCTAssertTrue(searchButton.waitForExistence(timeout: 20), "标签根页右上角应有搜索")
        searchButton.tap()
        let row = app.buttons.matching(NSPredicate(format: "identifier == 'history-row' AND label CONTAINS %@", torrentKeyword)).firstMatch
        guard row.waitForExistence(timeout: 15) else {
            throw XCTSkip("最近搜索里没有「\(torrentKeyword)」的站点资源快照，跳过（避免发起真实 PT 搜索）")
        }
        row.tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS '的快照'")).firstMatch.waitForExistence(timeout: 30), "应以快照回放，不打站点")
        XCTAssertTrue(app.buttons["torrent-row"].firstMatch.waitForExistence(timeout: 20))
        snapshot("站点资源-分组")

        // 排序：体积
        app.buttons["torrent-sort"].tap()
        app.buttons["体积"].tap()
        XCTAssertTrue(app.buttons["排序：体积降序"].waitForExistence(timeout: 5))

        // 视图：列表 → 图览 → 分组
        app.buttons["torrent-view-list"].tap()
        snapshot("站点资源-列表")
        app.buttons["torrent-view-poster"].tap()
        XCTAssertTrue(app.buttons["torrent-poster"].firstMatch.waitForExistence(timeout: 10) || app.buttons["torrent-row"].firstMatch.exists)
        snapshot("站点资源-图览")
        app.buttons["torrent-view-group"].tap()

        // 筛选弹层：选一个站点，回显条件后清除
        app.buttons["torrent-filter"].tap()
        let sheet = app.otherElements["torrent-filter-sheet"]
        XCTAssertTrue(sheet.waitForExistence(timeout: 5))
        sheet.scrollViews.firstMatch.buttons.firstMatch.tap()
        snapshot("筛选弹层")
        app.buttons["torrent-filter-done"].tap()
        XCTAssertTrue(app.otherElements["torrent-applied"].waitForExistence(timeout: 5) || app.staticTexts["生效条件"].waitForExistence(timeout: 5))
        app.buttons["清除全部"].firstMatch.tap()

        // 站点状态
        app.buttons["torrent-sites"].tap()
        XCTAssertTrue(app.navigationBars["站点搜索详情"].waitForExistence(timeout: 5))
        snapshot("站点状态")
        app.buttons["完成"].tap()

        // 资源操作面板 → 下载 → 保存位置对话框（只看预览，不确认）
        app.buttons["torrent-row"].firstMatch.tap()
        let download = app.buttons["torrent-action-download"]
        XCTAssertTrue(download.waitForExistence(timeout: 10), "管理员应看到「下载」")
        XCTAssertTrue(app.buttons["torrent-action-detail"].exists, "应有「查看详情」")
        snapshot("资源操作面板")
        download.tap()
        let confirmRemembered = app.buttons["download-confirm-remembered"]
        let confirm = app.buttons["download-confirm"]
        if confirmRemembered.waitForExistence(timeout: 5) {
            // 已有保存位置记忆：先看确认条，再点「更改」进完整对话框
            snapshot("保存位置确认条")
            app.buttons["更改"].tap()
        }
        XCTAssertTrue(confirm.waitForExistence(timeout: 15), "应弹出「选择保存位置」")
        let other = app.buttons["download-other-targets"]
        if other.waitForExistence(timeout: 20) { other.tap() }
        XCTAssertTrue(app.buttons["download-option-default"].waitForExistence(timeout: 20), "应列出下载器默认目录")
        snapshot("选择保存位置")
        app.buttons["取消"].tap()
        XCTAssertFalse(confirm.waitForExistence(timeout: 2))
    }
}
