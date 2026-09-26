import Foundation
import Testing
@testable import MovieClaw

/// 订阅首页（流媒体式版式）的纯逻辑：Hero 选哪几张、日程怎么分天、海报行怎么排与计数。
/// 模型用 JSON 构造（与真实接口同一套解码），时间全部相对固定的 `now` 生成，不依赖运行时区。
@MainActor
struct SubscriptionsHomeModelTests {
    private let now = Date(timeIntervalSince1970: 1_790_000_000)

    // MARK: 构造

    private func iso(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }

    private func decode<T: Decodable>(_ json: [String: Any]) throws -> T {
        try JSONDecoder().decode(T.self, from: JSONSerialization.data(withJSONObject: json))
    }

    private func media(_ id: Int, kind: String, status: String?) -> [String: Any] {
        [
            "media_item_id": id, "kind": kind, "tmdb_id": 1000 + id, "douban_id": NSNull(),
            "title": "作品\(id)", "original_title": "Title \(id)", "year": 2024,
            "poster_url": "https://image.tmdb.org/t/p/w500/p\(id).jpg",
            "backdrop_url": "https://image.tmdb.org/t/p/w1280/b\(id).jpg",
            "logo_url": NSNull(), "status": status ?? NSNull(),
        ]
    }

    private func sub(
        _ id: Int,
        kind: String = "tv",
        status: String = "active",
        mediaStatus: String? = "Returning Series",
        progress: [String: Int] = [:],
        owned: Int = 4,
        updatedAt: String = "2026-09-20T00:00:00Z"
    ) throws -> API.SubscriptionView {
        var counts = ["total": 8, "wanted": 1, "grabbed": 0, "downloaded": 0, "imported": 0, "upgrading": 0]
        counts.merge(progress) { $1 }
        return try decode([
            "id": id, "media": media(id, kind: kind, status: mediaStatus), "status": status,
            "selected_seasons": kind == "tv" ? [1] : [], "follow_future": kind == "tv", "rule_set_id": 1,
            "library_id": NSNull(), "progress": counts,
            "season_collection": kind == "tv"
                ? [["season_number": 1, "name": "第 1 季", "air_date": "2026-01-01", "episode_count": 8, "aired_count": 8, "owned_count": owned]]
                : [],
            "created_at": "2026-01-01T00:00:00Z", "updated_at": updatedAt,
        ])
    }

    private func arrival(
        sub: Int,
        kind: String = "tv",
        episode: Int = 1,
        status: String = "wanted",
        daysAhead: Int = 0,
        day: String = "2026-09-26",
        predictedAt: Date? = nil,
        downloadedAt: Date? = nil
    ) throws -> API.TodayArrivalView {
        var forecast: Any = NSNull()
        if let predictedAt {
            forecast = ["version": 3, "predicted_at": iso(predictedAt), "confidence": "high", "sites": []] as [String: Any]
        }
        return try decode([
            "subscription_id": sub, "wanted_id": sub * 100 + episode, "media_title": "作品\(sub)", "media_kind": kind,
            "season_number": kind == "tv" ? 1 : 0, "episode_number": kind == "tv" ? episode : 0, "status": status,
            "air_date": day, "expected_day": day, "days_ahead": daysAhead, "release_forecast": forecast,
            "next_probe_at": NSNull(), "info_hash": status == "wanted" ? NSNull() : "hash\(sub)",
            "grabbed_at": NSNull(), "downloaded_at": downloadedAt.map(iso) ?? NSNull(),
            "estimated_release_to_import_minutes": 60, "estimated_download_to_import_minutes": 10,
        ])
    }

    private func recent(sub: Int, kind: String = "tv", episodes: [Int] = [1], importedAt: Date) throws -> API.RecentArrivalView {
        try decode([
            "subscription_id": sub, "media": media(sub, kind: kind, status: "Returning Series"),
            "season_number": kind == "tv" ? 1 : 0, "episode_number": kind == "tv" ? episodes[0] : 0,
            "episode_name": kind == "tv" ? "第\(episodes[0])集" : NSNull(), "still_url": NSNull(),
            "units": episodes.map { ["season_number": kind == "tv" ? 1 : 0, "episode_number": kind == "tv" ? $0 : 0] },
            "progress_percent": NSNull(), "imported_at": iso(importedAt),
        ])
    }

    // MARK: Hero

    @Test func heroTellsWhatIsHappeningFirstThenWhatJustArrived() throws {
        let subs = try (1 ... 6).map { try sub($0) }
        let arrivals = try [
            arrival(sub: 1, status: "grabbed"),
            arrival(sub: 2, status: "downloaded", downloadedAt: now.addingTimeInterval(-60)),
            arrival(sub: 3, predictedAt: now.addingTimeInterval(2 * 3600)),
            arrival(sub: 4, daysAhead: 3, day: "2026-10-01"),
        ]
        let recents = try [
            recent(sub: 5, episodes: [3, 4], importedAt: now.addingTimeInterval(-2 * 3600)),
            recent(sub: 6, importedAt: now.addingTimeInterval(-3 * 86400)),
        ]
        let groups = SubscriptionsHome.arrivalGroups(arrivals, tasks: [], now: now)
        let slides = SubscriptionsHome.heroSlides(subscriptions: subs, groups: groups, recent: recents, now: now)

        // 整理中比下载中更快落地 → 48 小时内刚到的 → 今天的预告 → 更早到的；满 5 张为止，最远的预告挤不进来
        #expect(slides.map(\.subscriptionId) == [2, 1, 5, 3, 6])
        #expect(slides.map(\.stage) == [.organizing, .downloading, .arrived, .today, .arrived])
        // 整理中给出预计可看的时刻（下载完成 1 分钟前 + 历史耗时 10 分钟）
        #expect(slides[0].clock == SubscriptionsHome.clockText(now.addingTimeInterval(9 * 60), now: now))
        // 刚到的主按钮直接播放这一批里第一个没看完的单元，说明写「共 2 集新内容」
        #expect(slides[2].play == PlayRequest(mediaItemId: 5, season: 1, episode: 3))
        #expect(slides[2].eyebrow.text == "刚刚入库")
        #expect(slides[2].footnote?.contains("共 2 集新内容") == true)
        #expect(slides[4].eyebrow.text == "新一集")
        // 今天的预告：预测出种 + 历史耗时 60 分钟 = 预计入库时刻
        #expect(slides[3].clockLabel == "S01E01 · 预计入库")
        #expect(slides[3].clock == SubscriptionsHome.clockText(now.addingTimeInterval(3 * 3600), now: now))
    }

    @Test func heroUpcomingSpeaksInWeekdaysAndFallsBackToTrackedTitles() throws {
        let subs = try [sub(1, updatedAt: "2026-09-01T00:00:00Z"), sub(2, updatedAt: "2026-09-25T00:00:00Z"), sub(3, status: "completed")]
        let upcoming = SubscriptionsHome.heroSlides(
            subscriptions: subs,
            groups: SubscriptionsHome.arrivalGroups([try arrival(sub: 1, daysAhead: 5, day: "2026-10-01")], tasks: [], now: now),
            recent: [],
            now: now
        )
        #expect(upcoming.count == 1)
        #expect(upcoming[0].stage == .upcoming)
        #expect(upcoming[0].clock == "周四")
        #expect(upcoming[0].clockLabel == "S01E01 · 10月1日")

        // 什么都没发生：退回在追的几部（最近动过的在前），不编时间
        let resting = SubscriptionsHome.heroSlides(subscriptions: subs, groups: [], recent: [], now: now)
        #expect(resting.map(\.subscriptionId) == [2, 1, 3])
        #expect(resting.allSatisfy { $0.clock == nil && $0.play == nil })
        #expect(resting.last?.eyebrow.text == "已收齐")
    }

    // MARK: 日程

    @Test func scheduleIsAWeekStripWithTheEighthDayOnlyWhenBooked() throws {
        let subs = try [sub(1), sub(2), sub(3)]
        let arrivals = try [
            arrival(sub: 1, daysAhead: 0, day: "2026-09-26", predictedAt: now.addingTimeInterval(3600)),
            arrival(sub: 2, status: "grabbed", daysAhead: 0, day: "2026-09-26"),
            arrival(sub: 3, episode: 7, daysAhead: 7, day: "2026-10-03"),
            arrival(sub: 3, episode: 8, daysAhead: 7, day: "2026-10-03"),
        ]
        let days = SubscriptionsHome.scheduleDays(
            groups: SubscriptionsHome.arrivalGroups(arrivals, tasks: [], now: now), subscriptions: subs, now: now
        )
        #expect(days.map(\.daysAhead) == Array(0 ... 7))
        #expect(days.map(\.weekday) == ["今天", "周日", "周一", "周二", "周三", "周四", "周五", "周六"])
        #expect(days.map(\.dayNumber) == ["26", "27", "28", "29", "30", "1", "2", "3"])
        #expect(days[1 ... 6].allSatisfy { $0.entries.isEmpty })
        // 当天正在发生的排前面；给不出 ETA 的下载说「稍后」
        #expect(days[0].entries.map(\.subscriptionId) == [2, 1])
        #expect(days[0].entries[0].time == "稍后")
        #expect(days[0].entries[0].status == "下载中")
        // 同一部剧同一天的两集合成一行
        #expect(days[7].entries.count == 1)
        #expect(days[7].entries[0].episodeLabel == "S01E07–E08")

        let withoutEighth = SubscriptionsHome.scheduleDays(
            groups: SubscriptionsHome.arrivalGroups(Array(arrivals.prefix(2)), tasks: [], now: now), subscriptions: subs, now: now
        )
        #expect(withoutEighth.count == 7)
        #expect(SubscriptionsHome.scheduleDays(groups: [], subscriptions: subs, now: now).isEmpty)
    }

    // MARK: 海报行

    @Test func tvShelfPutsWhatIsInProgressFirstAndFinishedLast() throws {
        let subs = try [
            sub(1, owned: 8),
            sub(2),
            sub(3),
            sub(4, progress: ["upgrading": 2]),
            sub(5),
            sub(6, status: "paused"),
            sub(7, status: "completed", progress: ["imported": 8, "wanted": 0], owned: 8, updatedAt: "2026-09-01T00:00:00Z"),
            sub(8, status: "completed", progress: ["imported": 8, "wanted": 0], owned: 8),
            sub(9, owned: 8),
            sub(10, owned: 8),
            sub(11, status: "completed", progress: ["imported": 8, "wanted": 0, "upgrading": 1], owned: 8),
        ]
        let arrivals = try [
            arrival(sub: 1, daysAhead: 2, day: "2026-09-28"),
            arrival(sub: 2, status: "grabbed"),
            arrival(sub: 3, predictedAt: now.addingTimeInterval(3600)),
        ]
        let recents = try [
            recent(sub: 8, importedAt: now.addingTimeInterval(-3600)),
            recent(sub: 9, episodes: [5, 6], importedAt: now.addingTimeInterval(-7200)),
        ]
        let shelf = SubscriptionsHome.shelf(
            kind: "tv", subscriptions: subs,
            groups: SubscriptionsHome.arrivalGroups(arrivals, tasks: [], now: now), recent: recents
        )
        // 进行中：下载中 → 有没看的新集 → 今天更新 → 两天后更新 → 洗版中（含已收齐但正在洗版的）
        // → 缺集找资源 → 追更中（什么都不缺，等下一集）
        // 同名次按最近变动、再按片名（「作品11」排在「作品4」前）
        #expect(shelf.active.map(\.sub.id) == [2, 9, 3, 1, 11, 4, 5, 10])
        #expect(shelf.active.map { $0.chip?.text } == ["下载中", "新 2 集", "今天更新", "周一更新", "洗版中", "洗版中", "缺 4 集", nil])
        // 已完成的不因「刚到了、还没看」被拉回前排（那是 Hero 与刚刚入库的事）；最近完成的在前
        #expect(shelf.paused.map(\.sub.id) == [6])
        #expect(shelf.done.map(\.sub.id) == [8, 7])
        #expect(shelf.done.allSatisfy { $0.chip == nil && $0.progress == nil })
        #expect(shelf.restingLabel == "暂停·收齐")
        // 计数 = 分隔线前的数量
        #expect(SubscriptionsHome.countSummary(shelf) == "8 部进行中 · 共 11 部")
        #expect(shelf.active.first { $0.sub.id == 5 }?.meta == "第 1 季 · 4 / 8")
        #expect(shelf.active.first { $0.sub.id == 5 }?.progress == 0.5)
        #expect(shelf.done.first?.meta == "已收齐 · 第 1 季")
    }

    @Test func movieShelfFollowsTheSameOrderAndNeverPullsFinishedOnesBack() throws {
        let subs = try [
            sub(1, kind: "movie", mediaStatus: "Released"),
            sub(2, kind: "movie", mediaStatus: "Post Production"),
            sub(3, kind: "movie", status: "completed", mediaStatus: "Released", progress: ["imported": 1, "wanted": 0]),
            sub(4, kind: "movie", mediaStatus: "Released", progress: ["grabbed": 1, "wanted": 0]),
            sub(5, kind: "movie", status: "completed", mediaStatus: "Released", progress: ["imported": 1, "wanted": 0],
                updatedAt: "2026-09-25T00:00:00Z"),
        ]
        // 电影 5 刚入库、还没看：出现在 Hero 与刚刚入库那一排，但在海报行里已经算完成
        let recents = try [recent(sub: 5, kind: "movie", importedAt: now.addingTimeInterval(-3600))]
        let shelf = SubscriptionsHome.shelf(kind: "movie", subscriptions: subs, groups: [], recent: recents)
        // 没有预告时按订阅进度判断下载中；没上映的也算进行中，排在最后
        #expect(shelf.active.map(\.sub.id) == [4, 1, 2])
        #expect(shelf.active.map { $0.chip?.text } == ["下载中", "找资源中", "未上映"])
        #expect(shelf.done.map(\.sub.id) == [5, 3])
        #expect(shelf.done.map(\.meta) == ["2024 · 已入库", "2024 · 已入库"])
        #expect(shelf.restingLabel == "已入库")
        #expect(SubscriptionsHome.countSummary(shelf) == "3 部进行中 · 共 5 部")
    }

    // MARK: 格式

    @Test func clockTextOnlyNamesTheDayWhenItIsNotToday() {
        let calendar = Calendar.current
        let noon = calendar.date(bySettingHour: 12, minute: 0, second: 0, of: now) ?? now
        #expect(SubscriptionsHome.clockText(noon.addingTimeInterval(3600), now: noon) == "13:00")
        #expect(SubscriptionsHome.clockText(noon.addingTimeInterval(20 * 3600), now: noon) == "明天 08:00")
        #expect(SubscriptionsHome.weekday(of: "2026-09-26") == "周六")
    }
}
