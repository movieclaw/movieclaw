import Foundation
import Testing
@testable import MovieClaw

/// 站点资源结果的纯前端规则（排序 / 筛选 / 分面自排除计数 / 作品分组 / 标签）与 SSE 事件解析。
/// 口径逐条对照 Web `components/search-results.tsx`。
struct TorrentSearchLogicTests {
    /// 构造一条种子：只填测试关心的字段，其余给中性默认值
    private func hit(
        site: String = "s1", id: String, title: String = "raw", seeders: Int = 10, size: Int = 1_000,
        free: Bool = false, down: Double = 1, hr: Bool? = nil, upload: String? = nil,
        type: String? = "movie", zh: String? = "沙丘", en: String? = "Dune", year: Int? = 2021,
        seasons: [Int] = [], episodes: [Int] = [], complete: Bool? = nil, resolution: String? = "1080p",
        source: String? = nil, remux: Bool = false, subs: [String] = [], group: String? = nil
    ) -> API.TorrentHit {
        let attrs: [String: Any?] = [
            "media_type": type, "content_type": nil, "titles_zh": zh.map { [$0] } ?? [], "titles_en": en.map { [$0] } ?? [],
            "title_candidates": [], "year": year, "seasons": seasons, "episodes": episodes, "episodes_total": nil,
            "complete": complete, "resolution": resolution, "video_codec": nil, "hdr": [], "media_source": source,
            "remux": remux, "audio": [], "subtitle_languages": subs, "subtitle_carriers": [], "audio_languages": [],
            "platforms": [], "release_group": group,
        ]
        let json: [String: Any?] = [
            "torrent_id": id, "title": title, "subtitle": "", "category": "movie", "site_category_id": nil,
            "site_category_name": nil, "size": nil, "size_bytes": size, "seeders": seeders, "leechers": 0, "snatched": 0,
            "upload_time": upload, "uploader": "", "poster_url": nil, "image_urls": [], "free": free, "free_deadline": nil,
            "download_volume_factor": down, "upload_volume_factor": 1, "hit_and_run": hr, "detail_url": nil,
            "download_url": "https://x/\(id)", "site_id": site, "site_name": site.uppercased(),
            "attrs": attrs.mapValues { $0 ?? NSNull() },
        ]
        let data = try! JSONSerialization.data(withJSONObject: json.mapValues { $0 ?? NSNull() })
        return try! APIClient.decoder.decode(API.TorrentHit.self, from: data)
    }

    @Test func testSortBySeedersSizeAndDirection() {
        let a = hit(id: "a", seeders: 5, size: 300)
        let b = hit(id: "b", seeders: 50, size: 100)
        let c = hit(id: "c", seeders: 20, size: 200)
        #expect(TorrentSearchLogic.sorted([a, b, c], by: TorrentSort()).map(\.torrentId) == ["b", "c", "a"])
        #expect(TorrentSearchLogic.sorted([a, b, c], by: TorrentSort(key: .size, descending: false)).map(\.torrentId) == ["b", "c", "a"])
        // 点当前键翻转方向，点新键回到降序
        #expect(TorrentSort().picking(.seeders) == TorrentSort(key: .seeders, descending: false))
        #expect(TorrentSort(key: .seeders, descending: false).picking(.size) == TorrentSort(key: .size, descending: true))
    }

    @Test func testTimeSortPutsMissingLast() {
        let old = hit(id: "old", upload: "2024-01-01T00:00:00+00:00")
        let new = hit(id: "new", upload: "2026-01-01T00:00:00+00:00")
        let none = hit(id: "none", upload: nil)
        #expect(TorrentSearchLogic.sorted([none, old, new], by: TorrentSort(key: .time)).map(\.torrentId) == ["new", "old", "none"])
        #expect(TorrentSearchLogic.sorted([none, old, new], by: TorrentSort(key: .time, descending: false)).map(\.torrentId) == ["old", "new", "none"])
    }

    @Test func testSmartSorts() {
        let pack = hit(id: "pack", seeders: 1, type: "tv", seasons: [1])
        let single = hit(id: "single", seeders: 99, type: "tv", seasons: [1], episodes: [3])
        let complete = hit(id: "complete", seeders: 1, type: "tv", seasons: [1], episodes: [1, 2], complete: true)
        #expect(TorrentSearchLogic.sorted([single, pack, complete], by: TorrentSort(key: .complete)).map(\.torrentId) == ["complete", "pack", "single"])
        #expect(TorrentSearchLogic.smartSortKeys([single, pack]) == [.complete, .free])

        let uhd = hit(id: "uhd", seeders: 1, resolution: "2160p")
        let bluray = hit(id: "bd", seeders: 1, resolution: "1080p", source: "Blu-ray")
        let web = hit(id: "web", seeders: 50, resolution: "1080p", source: "WEB-DL")
        #expect(TorrentSearchLogic.sorted([web, bluray, uhd], by: TorrentSort(key: .quality)).map(\.torrentId) == ["uhd", "bd", "web"])
        #expect(TorrentSearchLogic.smartSortKeys([uhd, web]) == [.quality, .free])

        let free = hit(id: "free", seeders: 1, free: true)
        let half = hit(id: "half", seeders: 1, down: 0.5)
        let full = hit(id: "full", seeders: 100)
        #expect(TorrentSearchLogic.sorted([full, half, free], by: TorrentSort(key: .free)).map(\.torrentId) == ["free", "half", "full"])
    }

    @Test func testFiltersOrWithinAndAcrossDimensions() {
        let a = hit(site: "s1", id: "a", resolution: "2160p")
        let b = hit(site: "s2", id: "b", resolution: "1080p")
        let c = hit(site: "s2", id: "c", resolution: "2160p")
        var f = TorrentFilters()
        f.toggle(.resolution, "2160p")
        #expect([a, b, c].filter { TorrentSearchLogic.matches($0, f) }.map(\.torrentId) == ["a", "c"])
        f.toggle(.site, "s2")
        #expect([a, b, c].filter { TorrentSearchLogic.matches($0, f) }.map(\.torrentId) == ["c"])
        f.toggle(.resolution, "1080p") // 组内「或」
        #expect([a, b, c].filter { TorrentSearchLogic.matches($0, f) }.map(\.torrentId) == ["b", "c"])
        #expect(f.sheetCount == 1)
        f.toggle(.site, "s2")
        #expect(f.sheetCount == 0)
    }

    @Test func testEpisodeAndSubtitleSemantics() {
        let complete = hit(id: "complete", type: "tv", seasons: [1], complete: true)
        let e2 = hit(id: "e2", type: "tv", seasons: [1], episodes: [2])
        var f = TorrentFilters()
        f.toggle(.episode, "5")
        // 全集包视为包含任意一集
        #expect(TorrentSearchLogic.matches(complete, f))
        #expect(!(TorrentSearchLogic.matches(e2, f)))

        let hans = hit(id: "hans", subs: ["zh-Hans"])
        let generic = hit(id: "zh", subs: ["zh"])
        var zh = TorrentFilters()
        zh.toggle(.subtitle, "zh")
        #expect(TorrentSearchLogic.matches(hans, zh), "勾 zh 命中简体")
        var onlyHans = TorrentFilters()
        onlyHans.toggle(.subtitle, "zh-Hans")
        #expect(!(TorrentSearchLogic.matches(generic, onlyHans)), "勾简体不命中泛称中字")
    }

    @Test func testFacetCountsExcludeOwnDimension() {
        let a = hit(site: "s1", id: "a", resolution: "2160p")
        let b = hit(site: "s2", id: "b", resolution: "1080p")
        let c = hit(site: "s2", id: "c", resolution: "2160p")
        var f = TorrentFilters()
        f.toggle(.resolution, "2160p")
        let facets = TorrentSearchLogic.facets([a, b, c], filters: f)
        // 分辨率维度自身不设限：1080p 的数字 = 选中它之后能看到的条数
        #expect(facets.values(.resolution).first { $0.value == "1080p" }?.count == 1)
        #expect(facets.values(.resolution).first { $0.value == "2160p" }?.count == 2)
        // 站点维度受分辨率约束：s2 只剩 c
        #expect(facets.values(.site).first { $0.value == "s2" }?.count == 1)
    }

    @Test func testEntityGroupingAndBuckets() {
        let dune2021 = hit(id: "a", zh: "沙丘", year: 2021)
        let dune1984 = hit(id: "b", zh: "沙丘", year: 1984)
        let unparsed = hit(id: "u", zh: nil, en: nil)
        let dune2021b = hit(id: "c", zh: "沙丘", year: 2021)
        let buckets = TorrentSearchLogic.buckets([unparsed, dune2021, dune1984, dune2021b])
        #expect(buckets.map(\.rows.count) == [2, 1, 1])
        #expect(buckets.last?.key == TorrentSearchLogic.unparsedKey, "未识别桶沉底")
        #expect(TorrentSearchLogic.entities([dune2021, dune1984]).count == 2, "电影按年份区分")
        let s1 = hit(id: "s1", type: "tv", zh: "三体", year: 2023)
        let s2 = hit(id: "s2", type: "tv", zh: "三体", year: 2024)
        #expect(TorrentSearchLogic.entities([s1, s2]).count == 1, "剧集不按年拆")
    }

    @Test func testLabels() {
        let a = hit(id: "a", type: "tv", seasons: [1], episodes: [1, 2, 3], resolution: "2160p", subs: ["zh-Hans", "zh-Hant", "en"])
        #expect(TorrentSearchLogic.seasonEpLabel(a.attrs!) == "S01E01-E03")
        #expect(TorrentSearchLogic.compactSubtitleBadge(a.attrs!) == "字幕 简·繁·英")
        #expect(TorrentSearchLogic.seasonEpisodeChip(a.attrs)?.text == "第1季 · 第1-3集")
        #expect(TorrentSearchLogic.formatNumList([3, 1]) == "1、3")
        let promos = TorrentSearchLogic.promos(hit(id: "p", down: 0.5, hr: true))
        #expect(promos == [.discount("50%"), .hitAndRun])
    }

    @Test func testScopeEncodingRoundTrip() {
        let scope = SearchScope(label: "4K 电影", categories: ["movie", "bogus"], siteIds: ["mteam", "hdhome"], posterMode: true, skipHistory: true)
        let decoded = SearchScope(encoded: scope.encoded)
        #expect(decoded.label == "4K 电影")
        #expect(decoded.categories == ["movie"], "未知分类静默丢弃")
        #expect(decoded.siteIds == ["mteam", "hdhome"])
        #expect(decoded.posterMode && decoded.skipHistory)
        #expect(SearchScope.all.encoded == nil)
        #expect(SearchScope(encoded: nil) == .all)
        let items = scope.queryItems(keyword: "沙丘", page: 2)
        #expect(items.contains(URLQueryItem(name: "no_history", value: "true")))
        #expect(items.contains(URLQueryItem(name: "page", value: "2")))
    }

    @Test func testParseStreamEvents() throws {
        let start = ServerEvent(id: nil, event: "start", data: #"{"keyword":"k","label":null,"categories":[],"page":1,"sites":[{"site_id":"a","site_name":"A"}]}"#)
        guard case let .start(sites) = try APIClient.parseTorrentEvent(start) else { Issue.record("应解析为 start"); return }
        #expect(sites.map(\.siteId) == ["a"])
        let error = ServerEvent(id: nil, event: "site_error", data: #"{"site_id":"a","site_name":"A","error":"Cookie 已过期","elapsed_ms":1200}"#)
        guard case let .siteError(_, _, message, elapsed) = try APIClient.parseTorrentEvent(error) else { Issue.record("应解析为 site_error"); return }
        #expect(message == "Cookie 已过期")
        #expect(elapsed == 1200)
        let done = ServerEvent(id: nil, event: "done", data: #"{"total":0,"elapsed_ms":5,"sites":[]}"#)
        guard case .done = try APIClient.parseTorrentEvent(done) else { Issue.record("应解析为 done"); return }
        #expect(try APIClient.parseTorrentEvent(ServerEvent(id: nil, event: "ping", data: "{}")) == nil)
    }

    @Test func sseLineParserHandlesCRLFCommentsAndMultilineData() {
        var parser = SSELineParser()
        let lines = [": ping", "event: site_error\r", "data: {\"a\":", "data: 1}", ""]
        var events: [ServerEvent] = []
        for line in lines {
            if let event = parser.feed(Data(line.utf8)) { events.append(event) }
        }
        #expect(events.count == 1)
        #expect(events.first?.event == "site_error")
        #expect(events.first?.data == "{\"a\":\n1}")
        // 标题里的 U+2028 不影响：只按 LF 切行
        #expect(parser.feed(Data("data: {\"t\":\"a\u{2028}b\"}".utf8)) == nil)
        #expect(parser.feed(Data())?.data == "{\"t\":\"a\u{2028}b\"}")
    }
}
