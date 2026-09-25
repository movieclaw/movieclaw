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

nonisolated extension APIClient {
    /// 订阅 SSE 流（任务中心 `/jobs/stream`、资源搜索 `/search/torrents/stream`、
    /// AI 会话 `/sessions/{id}/events`）。携带会话 Cookie；调用方取消 Task 即断开。
    ///
    /// 按 SSE 规范解析：空行分隔事件，`event:` 缺省为 "message"，多行 `data:` 以换行拼接，
    /// `:` 开头是注释（后端心跳）直接忽略。
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
                    var id: String?
                    var event = "message"
                    var data: [String] = []
                    for try await line in bytes.lines {
                        if line.isEmpty {
                            if !data.isEmpty {
                                continuation.yield(ServerEvent(id: id, event: event, data: data.joined(separator: "\n")))
                            }
                            event = "message"
                            data = []
                            continue
                        }
                        if line.hasPrefix(":") { continue }
                        let (field, value) = Self.splitField(line)
                        switch field {
                        case "event": event = value
                        case "data": data.append(value)
                        case "id": id = value
                        default: break
                        }
                    }
                    // `bytes.lines` 会吞掉末尾空行：流结束时把最后一条也发出去
                    if !data.isEmpty {
                        continuation.yield(ServerEvent(id: id, event: event, data: data.joined(separator: "\n")))
                    }
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

    private static func splitField(_ line: String) -> (String, String) {
        guard let colon = line.firstIndex(of: ":") else { return (line, "") }
        let field = String(line[..<colon])
        var value = line[line.index(after: colon)...]
        if value.first == " " { value = value.dropFirst() }
        return (field, String(value))
    }
}
