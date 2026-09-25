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
