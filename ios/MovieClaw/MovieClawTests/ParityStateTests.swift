import Foundation
import Testing
@testable import MovieClaw

/// 第二轮对等审计的状态类问题（对等修补四）：
/// - N-04a-1 首页行偏好单例跨账号串用：换账号即作废旧副本、晚到的旧账号结果不落地；
/// - N-03-1 剧照「设为背景」：上传回显必须交给全站背景应用；
/// - N-05-4 / N-share-4 站内播放链接解析（同 Web play-links 的地址约定）。
@MainActor
struct ParityStateTests {
    private func row(_ id: String) -> API.HomeRowPref {
        API.HomeRowPref(id: id, sort: nil, order: nil, name: nil, unwatched: nil, hidden: nil, libraryId: nil, collectionId: nil)
    }

    private func ids(_ prefs: LibraryHomePrefs) -> [String]? {
        prefs.rows.map { rows in rows.map { $0.id } }
    }

    @Test func homePrefsDropRowsWhenAccountChanges() {
        let prefs = LibraryHomePrefs()
        prefs.adopt(owner: "http://nas/api|alice")
        prefs.accept([row("up_next"), row("favorites")], for: "http://nas/api|alice")
        #expect(ids(prefs) == ["up_next", "favorites"])

        // 同一账号再次使用：副本保留，不重拉
        prefs.adopt(owner: "http://nas/api|alice")
        #expect(prefs.rows?.count == 2)

        // 换账号：旧账号的清单作废（否则合集页「显示在首页」会以它为底整份覆盖新账号的偏好）
        prefs.adopt(owner: "http://nas/api|bob")
        #expect(prefs.rows == nil)
        #expect(prefs.owner == "http://nas/api|bob")
    }

    @Test func homePrefsIgnoreLateResultOfPreviousAccount() {
        let prefs = LibraryHomePrefs()
        prefs.adopt(owner: "http://nas/api|alice")
        // 拉取期间切到了 bob：alice 的结果晚到，不能落到 bob 名下
        prefs.adopt(owner: "http://nas/api|bob")
        prefs.accept([row("libraries")], for: "http://nas/api|alice")
        #expect(prefs.rows == nil)
        prefs.accept([row("favorites")], for: "http://nas/api|bob")
        #expect(ids(prefs) == ["favorites"])
        // 已有副本时不被一次迟到的拉取覆盖（自定义页刚保存的更新）
        prefs.accept([row("up_next")], for: "http://nas/api|bob")
        #expect(ids(prefs) == ["favorites"])
    }

    @Test func setBackdropAppliesUploadedAppearance() async throws {
        let uploaded = API.AppearanceView(activeId: "abc", activeUrl: "/api/v1/appearance/backdrops/abc?v=2", backdrops: [])
        var uploadedIndex: Int?
        var applied: API.AppearanceView?
        let action = MediaDetailView.setBackdropAction(
            upload: { index in
                uploadedIndex = index
                return uploaded
            },
            apply: { applied = $0 }
        )
        #expect(action.label == "设为背景")
        try await action.run(3)
        #expect(uploadedIndex == 3)
        #expect(applied == uploaded)
    }

    @Test func setBackdropDoesNotApplyWhenUploadFails() async {
        struct Boom: Error {}
        var applied = false
        let action = MediaDetailView.setBackdropAction(upload: { _ in throw Boom() }, apply: { _ in applied = true })
        await #expect(throws: Boom.self) { try await action.run(0) }
        #expect(!applied)
    }

    @Test func playLinksParse() {
        let movie = PlayRequest(webPath: "/play/6506?t=600")
        #expect(movie?.mediaItemId == 6506)
        #expect(movie?.season == nil)
        #expect(movie?.startSeconds == 600)

        let episode = PlayRequest(webPath: "/play/12/s01e02")
        #expect(episode?.season == 1)
        #expect(episode?.episode == 2)
        #expect(episode?.startSeconds == nil)

        // s00e00 = 电影；非法 t 按缺失处理（同 Web queryNumber）
        let zero = PlayRequest(webPath: "/play/7/s00e00?t=-5")
        #expect(zero?.season == nil)
        #expect(zero?.startSeconds == nil)

        let share = PlayRequest(webPath: "/s/zZAUQX/play/s02e10?t=30")
        #expect(share?.shareSlug == "zZAUQX")
        #expect(share?.season == 2)
        #expect(share?.episode == 10)
        #expect(share?.startSeconds == 30)

        #expect(PlayRequest(webPath: "/s/zZAUQX") == nil)
        #expect(PlayRequest(webPath: "/library/19") == nil)
        #expect(PlayRequest(webPath: "/play/abc") == nil)
    }

    @Test func settingsSectionBackChainGoesThroughSettingsList() {
        let router = Router()
        router.selectedTab = .library
        router.open(.settingsSection(.app))
        #expect(router.paths[.library] == [.settings, .settingsSection(.app)])
        // 从一个分区直达另一个分区：返回同样先回设置列表
        router.open(.settingsSection(.playback))
        let tail = Array((router.paths[.library] ?? []).suffix(2))
        #expect(tail == [.settings, .settingsSection(.playback)])
    }
}
