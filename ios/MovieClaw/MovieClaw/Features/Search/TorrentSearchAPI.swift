import Foundation

/// 站点资源流式搜索 `GET /search/torrents/stream`（SSE，生成器未生成，这里手写）。
///
/// 事件序列：`start → site_start × N → (site_result | site_error) × N → done`，
/// 快的站点先出结果。事件载荷在后台解码（一个站点一次可能上百条），调用方拿到的是强类型事件。
/// 与 Web 一样用一次性流而不是会自动重连的 EventSource：搜索是一次性动作，失败由用户显式重试。
nonisolated enum TorrentStreamEvent: Sendable {
    case start(sites: [TorrentStreamSite])
    case siteStart(TorrentStreamSite)
    case siteResult(siteId: String, siteName: String, count: Int, elapsedMs: Int, items: [API.TorrentHit])
    case siteError(siteId: String, siteName: String, error: String, elapsedMs: Int)
    case done(total: Int, elapsedMs: Int, sites: [API.SiteSearchStatus])
}

nonisolated struct TorrentStreamSite: Decodable, Hashable, Sendable {
    var siteId: String
    var siteName: String

    enum CodingKeys: String, CodingKey {
        case siteId = "site_id"
        case siteName = "site_name"
    }
}

private nonisolated struct StreamStartPayload: Decodable {
    var sites: [TorrentStreamSite]
}

private nonisolated struct StreamResultPayload: Decodable {
    var siteId: String
    var siteName: String
    var count: Int
    var elapsedMs: Int
    var items: [API.TorrentHit]

    enum CodingKeys: String, CodingKey {
        case siteId = "site_id"
        case siteName = "site_name"
        case count
        case elapsedMs = "elapsed_ms"
        case items
    }
}

private nonisolated struct StreamErrorPayload: Decodable {
    var siteId: String
    var siteName: String
    var error: String
    var elapsedMs: Int

    enum CodingKeys: String, CodingKey {
        case siteId = "site_id"
        case siteName = "site_name"
        case error
        case elapsedMs = "elapsed_ms"
    }
}

private nonisolated struct StreamDonePayload: Decodable {
    var total: Int
    var elapsedMs: Int
    var sites: [API.SiteSearchStatus]

    enum CodingKeys: String, CodingKey {
        case total
        case elapsedMs = "elapsed_ms"
        case sites
    }
}

nonisolated extension APIClient {
    /// 流式跨站搜索；取消消费方的 Task 即断开连接
    func torrentSearchStream(keyword: String, scope: SearchScope, page: Int = 1) -> AsyncThrowingStream<TorrentStreamEvent, Error> {
        let source = events("/search/torrents/stream", query: scope.queryItems(keyword: keyword, page: page))
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    for try await event in source {
                        if let parsed = try Self.parseTorrentEvent(event) { continuation.yield(parsed) }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// 把一条 SSE 事件解码为强类型事件；未知事件（心跳等）返回 nil
    static func parseTorrentEvent(_ event: ServerEvent) throws -> TorrentStreamEvent? {
        do {
            switch event.event {
            case "start":
                return .start(sites: try event.decode(StreamStartPayload.self).sites)
            case "site_start":
                return .siteStart(try event.decode(TorrentStreamSite.self))
            case "site_result":
                let p = try event.decode(StreamResultPayload.self)
                return .siteResult(siteId: p.siteId, siteName: p.siteName, count: p.count, elapsedMs: p.elapsedMs, items: p.items)
            case "site_error":
                let p = try event.decode(StreamErrorPayload.self)
                return .siteError(siteId: p.siteId, siteName: p.siteName, error: p.error, elapsedMs: p.elapsedMs)
            case "done":
                let p = try event.decode(StreamDonePayload.self)
                return .done(total: p.total, elapsedMs: p.elapsedMs, sites: p.sites)
            default:
                return nil
            }
        } catch {
            throw APIError.decoding(APIClient.describe(error))
        }
    }

    /// 读取一条 PT 搜索历史的结果快照
    func torrentSearchSnapshot(historyId: Int) async throws -> API.TorrentSearchHistoryResultsView {
        let value = try await searchHistoryGetResults(historyId: historyId)
        guard value["vertical"]?.stringValue == "torrents" else { throw APIError.network("该历史记录不是 PT 种子搜索") }
        return try value.decode(as: API.TorrentSearchHistoryResultsView.self)
    }

    /// 读取一条影视搜索历史的结果快照（旧快照条目是 MediaSearchItem：id / source / title / year / type / rating / poster_url）
    func titleSearchSnapshot(historyId: Int) async throws -> (snapshotAt: String, items: [DiscoverPosterItem]) {
        let value = try await searchHistoryGetResults(historyId: historyId)
        guard value["vertical"]?.stringValue == "titles" else { throw APIError.network("该历史记录不是影视条目搜索") }
        let items: [DiscoverPosterItem] = (value["items"]?.arrayValue ?? []).compactMap { item in
            guard let id = item["id"]?.stringValue, let title = item["title"]?.stringValue else { return nil }
            return DiscoverPosterItem(
                titleRef: item["title_ref"]?.stringValue,
                externalId: id,
                source: item["source"]?.stringValue ?? "tmdb",
                mediaType: item["type"]?.stringValue,
                title: title,
                year: item["year"]?.intValue,
                rating: item["rating"]?.doubleValue ?? 0,
                posterUrl: item["poster_url"]?.stringValue
            )
        }
        return (value["snapshot_at"]?.stringValue ?? "", items)
    }
}
