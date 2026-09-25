import Foundation
import Testing
@testable import MovieClaw

/// 发现模块的纯逻辑：站内链接恢复视角（类型 × 数据源 × 筛选，同 Web `/discover/{type}?…`）
@MainActor
struct DiscoverLogicTests {
    @Test func viewpointFromTypeOnly() {
        let viewpoint = DiscoverViewpoint(parameter: "tv")
        #expect(viewpoint.mediaType == "tv")
        #expect(viewpoint.source == "tmdb")
        #expect(viewpoint.filters == .empty)
        #expect(DiscoverViewpoint(parameter: "bogus").mediaType == "movie")
    }

    @Test func viewpointRestoresSourceAndFilters() {
        let viewpoint = DiscoverViewpoint(parameter: "movie?genres=28,12,28,-1&country=us&year=2024&rating=7.5&runtime=120&sort=rating")
        #expect(viewpoint.source == "tmdb")
        #expect(viewpoint.filters.genreIds == [28, 12])
        #expect(viewpoint.filters.originCountry == "US")
        #expect(viewpoint.filters.year == 2024)
        #expect(viewpoint.filters.ratingGte == 7.5)
        #expect(viewpoint.filters.runtimeLte == 120)
        #expect(viewpoint.filters.sort == "rating")
        #expect(viewpoint.filters.activeCount == 6)
    }

    @Test func invalidFilterValuesAreIgnored() {
        let filters = DiscoverViewpoint(parameter: "tv?year=1800&rating=11&runtime=0&sort=hot&country=USA").filters
        #expect(filters == .empty)
        // 豆瓣视角不带筛选（筛选仅 TMDB 支持）
        let douban = DiscoverViewpoint(parameter: "tv?source=douban&genres=18")
        #expect(douban.source == "douban")
        #expect(douban.filters == .empty)
    }
}
