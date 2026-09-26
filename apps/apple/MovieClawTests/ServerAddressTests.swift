import Testing
@testable import MovieClaw

struct ServerAddressTests {
    @Test(arguments: [
        ("192.168.1.10:3000", "http://192.168.1.10:3000"),
        ("  http://192.168.1.10:3000/  ", "http://192.168.1.10:3000"),
        ("http://192.168.1.10:3000/login?next=%2F", "http://192.168.1.10:3000"),
        ("https://Movie.Example.com/library/3", "https://movie.example.com"),
        ("https://movie.example.com:443", "https://movie.example.com"),
        ("http://nas.local:80", "http://nas.local"),
        ("movie.ycy.homes:88", "http://movie.ycy.homes:88"),
    ])
    func normalizes(input: String, expected: String) throws {
        #expect(try ServerAddress(parsing: input).origin.absoluteString == expected)
    }

    @Test(arguments: ["", "   ", "ftp://host", "http://", "://x"])
    func rejects(input: String) {
        #expect(throws: ServerAddress.ParseError.self) { try ServerAddress(parsing: input) }
    }

    @Test func apiBaseAndResolve() throws {
        let address = try ServerAddress(parsing: "http://nas:3000")
        #expect(address.apiBase.absoluteString == "http://nas:3000/api/v1")
        #expect(address.resolve("/api/v1/images/a.jpg")?.absoluteString == "http://nas:3000/api/v1/images/a.jpg")
        #expect(address.resolve("https://image.tmdb.org/x.jpg")?.absoluteString == "https://image.tmdb.org/x.jpg")
        #expect(address.resolve(nil) == nil)
    }

    @Test func clientURLMergesQuery() throws {
        let client = APIClient(server: try ServerAddress(parsing: "http://nas:3000"))
        let url = client.url("/libraries/1/items?page=2", query: [.init(name: "sort", value: "added")])
        #expect(url.absoluteString == "http://nas:3000/api/v1/libraries/1/items?page=2&sort=added")
    }
}

struct AppRouteParsingTests {
    @Test(arguments: [
        ("/library/19/item/1019?season=10&episode=1", AppRoute.libraryItem(libraryId: 19, itemId: 1019, season: 10, episode: 1)),
        ("/media/movie/550", AppRoute.mediaDetail(titleRef: "tmdb:movie:550")),
        ("/subscriptions/12?upgrade-run=1", AppRoute.subscription(id: 12, upgradeRun: true)),
        ("/tasks?view=history", AppRoute.activity(view: "history")),
        ("/settings/about", AppRoute.settingsSection(.app)),
        ("/settings/app?tab=remote", AppRoute.settingsSection(.playback)),
        ("/discover/movie/top250", AppRoute.discoverCollection(kind: "movie", provider: "douban", collectionId: "movie_top250")),
        ("/library/c/7", AppRoute.collection(libraryId: nil, collectionId: 7)),
        ("/library/19?view=collections&pending=1", AppRoute.library(id: 19, view: "collections", pending: true)),
        ("/library/manage?tab=duplicates&item=6434", AppRoute.libraryManage(create: false, tab: "duplicates", item: 6434)),
        ("/settings/app?tab=storage", AppRoute.settingsSection(.app, query: ["tab": "storage"])),
        ("/discover/tv?source=douban", AppRoute.discover(kind: "tv?source=douban")),
    ])
    func parses(path: String, expected: AppRoute) {
        #expect(AppRoute(webPath: path) == expected)
    }

    @Test func searchFoldsScopeParams() throws {
        guard case let .search(query)? = AppRoute(webPath: "/search?q=%E5%A5%A5%E6%9C%AC&cats=movie&sites=1,2") else {
            Issue.record("未解析为搜索路由"); return
        }
        #expect(query.q == "奥本")
        #expect(query.scope != nil)
    }
}
