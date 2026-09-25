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

/// SSE 逐行解析：空行分隔事件，`event:` 缺省为 message，多行 `data:` 以换行拼接，`:` 开头是注释
nonisolated struct SSELineParser {
    private var event = "message"
    private var data: [String] = []

    /// 喂入一行（不含 LF，可能带 CR）；遇到空行时返回凑齐的事件
    mutating func feed(_ raw: Data) -> ServerEvent? {
        var bytes = raw
        if bytes.last == 0x0D { bytes.removeLast() }
        if bytes.isEmpty {
            defer { event = "message"; data = [] }
            return data.isEmpty ? nil : ServerEvent(id: nil, event: event, data: data.joined(separator: "\n"))
        }
        let line = String(decoding: bytes, as: UTF8.self)
        if line.hasPrefix(":") { return nil }
        let field: String
        var value: Substring
        if let colon = line.firstIndex(of: ":") {
            field = String(line[..<colon])
            value = line[line.index(after: colon)...]
            if value.first == " " { value = value.dropFirst() }
        } else {
            field = line
            value = ""
        }
        switch field {
        case "event": event = String(value)
        case "data": data.append(String(value))
        default: break
        }
        return nil
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
    /// 流式跨站搜索；取消消费方的 Task 即断开连接。
    ///
    /// 这里没有复用通用的 `events(_:)`（按 `AsyncBytes.lines` 切行）：联调 NAS 实测，同一次「奥本海默」
    /// 搜索用 lines 切行时 `site_result` 解析报「数据损坏」（上百 KB 的单行被切断），同样的数据落盘后
    /// 用 lines 读却正常，推测与网络分块边界或 lines 把 Unicode 换行符（U+2028 等）也当行尾有关。
    /// SSE 规范只认 LF / CR / CRLF，这里按字节切行，换成它后同一搜索正常流式出结果。
    func torrentSearchStream(keyword: String, scope: SearchScope, page: Int = 1) -> AsyncThrowingStream<TorrentStreamEvent, Error> {
        var request = URLRequest(url: url("/search/torrents/stream", query: scope.queryItems(keyword: keyword, page: page)))
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 3600
        let session = self.session
        let finalRequest = request
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, response) = try await session.bytes(for: finalRequest)
                    guard let http = response as? HTTPURLResponse else { throw APIError.network("服务器响应异常") }
                    guard (200 ..< 300).contains(http.statusCode) else {
                        var body = Data()
                        for try await byte in bytes { body.append(byte); if body.count > 64_000 { break } }
                        let message = (try? JSONSerialization.jsonObject(with: body) as? [String: Any])?["message"] as? String
                        if http.statusCode == 401 { NotificationCenter.default.post(name: .apiUnauthorized, object: nil) }
                        throw APIError.http(status: http.statusCode, message: message ?? "请求失败（HTTP \(http.statusCode)）", code: nil)
                    }
                    var parser = SSELineParser()
                    var line = Data()
                    for try await byte in bytes {
                        if byte == 0x0A {
                            if let event = parser.feed(line), let parsed = try Self.parseTorrentEvent(event) { continuation.yield(parsed) }
                            line.removeAll(keepingCapacity: true)
                        } else {
                            line.append(byte)
                        }
                    }
                    if !line.isEmpty, let event = parser.feed(line), let parsed = try Self.parseTorrentEvent(event) { continuation.yield(parsed) }
                    if let event = parser.feed(Data()), let parsed = try Self.parseTorrentEvent(event) { continuation.yield(parsed) }
                    continuation.finish()
                } catch let error as URLError where error.code == .cancelled {
                    continuation.finish()
                } catch let error as URLError {
                    continuation.finish(throwing: APIError.network(APIClient.networkMessage(error)))
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
