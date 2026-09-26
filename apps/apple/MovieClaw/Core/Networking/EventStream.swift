import Foundation

/// 一条 Server-Sent Event
nonisolated struct ServerEvent: Sendable {
    var id: String?
    var event: String
    var data: String

    /// 把 data 解码为 JSON 模型
    func decode<T: Decodable>(_ type: T.Type = T.self) throws -> T {
        try APIClient.decoder.decode(T.self, from: Data(data.utf8))
    }
}

/// SSE 逐行解析器：空行分隔事件，`event:` 缺省为 "message"，多行 `data:` 以换行拼接，
/// `id:` 记录最后事件号，`:` 开头是注释（后端心跳）直接忽略。
///
/// **必须按字节切行**，不能用 `AsyncBytes.lines`：后者会把 Unicode 行分隔符
/// （U+2028 / U+2029 / U+0085）也当行尾，而后端 JSON 以 ensure_ascii=False 输出，
/// 种子标题、AI 回复里一旦出现这些字符，单条事件就会被切断、解码报「数据损坏」
/// （NAS 联调「奥本海默」站点搜索实测复现）。SSE 规范只认 LF / CR / CRLF。
nonisolated struct SSELineParser {
    private var id: String?
    private var event = "message"
    private var data: [String] = []

    /// 喂入一行（不含 LF，可能带 CR）；遇到空行时返回凑齐的事件
    mutating func feed(_ raw: Data) -> ServerEvent? {
        var bytes = raw
        if bytes.last == 0x0D { bytes.removeLast() }
        if bytes.isEmpty { return flush() }
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
        case "id": id = String(value)
        default: break
        }
        return nil
    }

    /// 流结束时把最后一条（没有尾随空行的）事件也交出去
    mutating func flush() -> ServerEvent? {
        defer { event = "message"; data = [] }
        return data.isEmpty ? nil : ServerEvent(id: id, event: event, data: data.joined(separator: "\n"))
    }
}

nonisolated extension APIClient {
    /// 订阅 SSE 流（任务中心 `/jobs/stream`、资源搜索 `/search/torrents/stream`、
    /// AI 会话 `/sessions/{id}/events`）。携带会话 Cookie；调用方取消 Task 即断开。
    func events(_ path: String, query: [URLQueryItem] = [], lastEventId: String? = nil) -> AsyncThrowingStream<ServerEvent, Error> {
        var request = URLRequest(url: url(path, query: query))
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 3600
        if let lastEventId { request.setValue(lastEventId, forHTTPHeaderField: "Last-Event-ID") }
        let session = self.session
        let finalRequest = request

        return AsyncThrowingStream { continuation in
            let task = Task { @Sendable in
                do {
                    let (bytes, response) = try await session.bytes(for: finalRequest)
                    guard let http = response as? HTTPURLResponse else {
                        throw APIError.network("服务器响应异常")
                    }
                    guard (200 ..< 300).contains(http.statusCode) else {
                        var body = Data()
                        for try await byte in bytes { body.append(byte); if body.count > 64_000 { break } }
                        let message = (try? JSONSerialization.jsonObject(with: body) as? [String: Any])?["message"] as? String
                        if http.statusCode == 401 {
                            NotificationCenter.default.post(name: .apiUnauthorized, object: nil)
                        }
                        throw APIError.http(status: http.statusCode, message: message ?? "请求失败（HTTP \(http.statusCode)）", code: nil)
                    }
                    var parser = SSELineParser()
                    var line = Data()
                    for try await byte in bytes {
                        if byte == 0x0A {
                            if let event = parser.feed(line) { continuation.yield(event) }
                            line.removeAll(keepingCapacity: true)
                        } else {
                            line.append(byte)
                        }
                    }
                    if !line.isEmpty, let event = parser.feed(line) { continuation.yield(event) }
                    if let event = parser.flush() { continuation.yield(event) }
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
}
