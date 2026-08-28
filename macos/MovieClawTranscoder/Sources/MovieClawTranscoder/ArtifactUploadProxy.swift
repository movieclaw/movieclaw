import Foundation
import Network

enum ChunkedBodyParseResult: Equatable {
    case incomplete
    case complete(Data)
    case invalid(String)
    case tooLarge
}

/// 增量解析 HTTP chunked body。
///
/// ``Data`` 在调用 ``removeFirst`` 后可能保留非零的 ``startIndex``；因此所有
/// 按搜索结果消费的内容都必须使用真实索引范围删除，不能把 ``upperBound`` 当
/// 成从零开始的字节数量。这个状态机也让 TCP 任意拆包都能用纯单测覆盖。
struct ChunkedBodyParser {
    private static let crlf = Data([13, 10])
    private static let headerTerminator = Data([13, 10, 13, 10])
    private static let maxChunkLineBytes = 64 * 1024

    private let maxBodyBytes: Int
    private var buffer = Data()
    private var body = Data()
    private var chunkRemaining: Int?
    private var awaitingTrailers = false
    private var finished = false

    init(maxBodyBytes: Int) {
        self.maxBodyBytes = max(0, maxBodyBytes)
    }

    mutating func append(_ data: Data) -> ChunkedBodyParseResult {
        if finished {
            return .complete(body)
        }
        if !data.isEmpty {
            buffer.append(data)
        }
        return consume()
    }

    private mutating func consume() -> ChunkedBodyParseResult {
        while !finished {
            if awaitingTrailers {
                // 零长度块后没有 trailer 时只剩一个 CRLF；有 trailer 时等待
                // trailer 末尾的空行。两种情况都可能被 TCP 拆成多个包。
                if buffer.count >= 2,
                   Data(buffer.prefix(2)) == Self.crlf
                {
                    buffer.removeFirst(2)
                    finished = true
                    return .complete(body)
                }
                if let trailerEnd = buffer.range(of: Self.headerTerminator) {
                    removePrefix(upTo: trailerEnd.upperBound)
                    finished = true
                    return .complete(body)
                }
                if buffer.count > Self.maxChunkLineBytes {
                    return .invalid("chunked body 的 trailer 过长")
                }
                return .incomplete
            }

            if let remaining = chunkRemaining {
                guard buffer.count >= remaining + 2 else {
                    return .incomplete
                }
                let chunk = Data(buffer.prefix(remaining))
                buffer.removeFirst(remaining)
                guard Data(buffer.prefix(2)) == Self.crlf else {
                    return .invalid("chunked body 缺少结束换行")
                }
                buffer.removeFirst(2)
                guard chunk.count <= maxBodyBytes - body.count else {
                    return .tooLarge
                }
                body.append(chunk)
                chunkRemaining = nil
                continue
            }

            guard let lineRange = buffer.range(of: Self.crlf) else {
                if buffer.count > Self.maxChunkLineBytes {
                    return .invalid("chunked body 的块大小行过长")
                }
                return .incomplete
            }
            let lineData = Data(buffer[buffer.startIndex..<lineRange.lowerBound])
            removePrefix(upTo: lineRange.upperBound)
            let sizeText = String(decoding: lineData, as: UTF8.self)
                .split(separator: ";", maxSplits: 1, omittingEmptySubsequences: true)
                .first
                .map(String.init) ?? ""
            guard let size = Int(sizeText, radix: 16), size >= 0 else {
                return .invalid("chunked body 的块大小无效")
            }
            if size == 0 {
                awaitingTrailers = true
                continue
            }
            guard size <= maxBodyBytes - body.count else {
                return .tooLarge
            }
            chunkRemaining = size
        }
        return .complete(body)
    }

    private mutating func removePrefix(upTo end: Data.Index) {
        buffer.removeSubrange(buffer.startIndex..<end)
    }
}

/// 为一个 ffmpeg 任务提供仅监听回环地址的 HLS 产物代理。
///
/// ffmpeg 的 HTTP HLS muxer 在 PUT 请求中使用 chunked body，底层连接中断后
/// 无法由 Worker 重新发送已经生成的那一个产物。代理先把单个产物保存在
/// 内存中，再使用 URLSession 以固定 Content-Length 上传 NAS，并对网络错误与
/// 临时 HTTP 状态做有限次数重试。媒体数据从未写入 Worker 磁盘。
final class ArtifactUploadProxy: @unchecked Sendable {
    struct UploadResult: Sendable {
        let statusCode: Int
        let message: String?
        let attempts: Int

        var succeeded: Bool {
            (200..<300).contains(statusCode)
        }
    }

    private enum ProxyError: Error, CustomStringConvertible {
        case stopped
        case listenerFailed(String)

        var description: String {
            switch self {
            case .stopped:
                return "上传代理已停止"
            case let .listenerFailed(message):
                return "上传代理启动失败：\(message)"
            }
        }
    }

    private static let maxHeaderBytes = 64 * 1024
    /// 与服务端的默认值和校验上限保持一致；正常 HLS 分片远小于此值。
    static let maxArtifactBytes = 512 * 1024 * 1024
    private static let maxUploadAttempts = 3
    private static let uploadDrainTimeoutNanoseconds: UInt64 = 10_000_000_000
    private static let retryableStatusCodes: Set<Int> = [408, 425, 429, 499, 500, 502, 503, 504]
    private static let headerTerminator = Data([13, 10, 13, 10])
    private static let artifactNamePattern = try! NSRegularExpression(
        pattern: #"^(?:init\.mp4|(?:live|index)\.m3u8|seg[0-9]{5}\.m4s)$"#
    )

    /// VOD 会话里 ffmpeg 每写完一个分片都会重写一次它，但服务端对远程会话
    /// **不解析**这份列表（分片是否就绪以产物文件本身为准，见 NAS 侧
    /// `_sync_completed`）。每次重写都实传等于把上传请求数翻倍，却只是在送一份
    /// 对端不看的数据，所以这里只留最后一份、在任务收尾时补传一次备诊断。
    ///
    /// `index.m3u8` 不在此列：非 VOD 会话的这份列表要直接发给浏览器，服务端
    /// 起播时还会阻塞等它出现，必须实时上传。
    private static let deferredPlaylistName = "live.m3u8"

    /// 该产物是否攒到任务收尾再传。
    ///
    /// 判据只认 `live.m3u8` 这一个名字，**不能放宽成「所有 .m3u8」**：
    /// `index.m3u8` 是非 VOD 会话直接发给浏览器的列表，服务端起播时还会阻塞
    /// 等它出现，延迟上传会让这类会话直接起播失败。
    static func isDeferredArtifact(_ filename: String) -> Bool {
        filename == deferredPlaylistName
    }

    let jobID: String
    private let remoteBaseURL: URL
    private let queue: DispatchQueue
    private let listener: NWListener
    private let lock = NSLock()
    private var startContinuation: CheckedContinuation<URL, Error>?
    private var started = false
    private var stopped = false
    private var failureMessage: String?
    private var connections: [ObjectIdentifier: UploadConnection] = [:]
    /// 最后一次收到的 live.m3u8（内容与 query），收尾时补传。
    private var deferredPlaylist: (data: Data, query: String?)?
    /// 整个任务共用一条 URLSession：HLS 分片按秒级节奏产出，每个产物新建
    /// 会话会让每次上传都重做一遍 TCP + TLS 握手，握手的 RTT 恰好全落在
    /// 起播和 seek 这些最怕延迟的时刻。共用后连接可以 keep-alive 复用。
    private let uploadSession: URLSession

    init(jobID: String, remoteBaseURL: URL) throws {
        self.jobID = jobID
        self.remoteBaseURL = remoteBaseURL
        self.queue = DispatchQueue(label: "com.movieclaw.transcoder.artifacts.\(jobID)")

        // 会放在会抛错的那步之后：URLSession 在 invalidate 前会自持引用，
        // 若 listener 创建失败，先建好的会话没人来释放。
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = NWEndpoint.hostPort(
            host: "127.0.0.1",
            port: NWEndpoint.Port(rawValue: 0)!
        )
        self.listener = try NWListener(using: parameters)

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 90
        configuration.httpShouldSetCookies = false
        configuration.httpCookieStorage = nil
        // 分片上传之间要复用连接，这个上限决定了并发上传能占多少条
        configuration.httpMaximumConnectionsPerHost = 4
        self.uploadSession = URLSession(configuration: configuration)
    }

    /// 启动回环 HTTP 服务，并返回给 ffmpeg 使用的根地址。
    func start() async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            if stopped {
                lock.unlock()
                continuation.resume(throwing: ProxyError.stopped)
                return
            }
            if started || startContinuation != nil {
                lock.unlock()
                continuation.resume(throwing: ProxyError.listenerFailed("重复启动"))
                return
            }
            startContinuation = continuation
            lock.unlock()

            listener.stateUpdateHandler = { [weak self] state in
                self?.handleListenerState(state)
            }
            listener.newConnectionHandler = { [weak self] connection in
                self?.accept(connection)
            }
            listener.start(queue: queue)
        }
    }

    /// 把服务端生成的远程产物 URL 改写成回环代理 URL；源文件 URL 保持不变。
    func rewrite(arguments: [String], localBaseURL: URL) -> [String] {
        let remotePrefix = remoteBaseURL.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/"
        let localPrefix = localBaseURL.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/"

        return arguments.map { argument in
            if argument.hasPrefix(remotePrefix) {
                return localPrefix + String(argument.dropFirst(remotePrefix.count))
            }
            // 服务端为 init.mp4 传的是相对文件名；显式改成回环地址，避免
            // 不同 ffmpeg 版本对相对 URL 的基准解析不一致。
            if argument == "init.mp4" || argument.hasPrefix("init.mp4?") {
                return localPrefix + argument
            }
            return argument
        }
    }

    /// 从服务端下发的 ffmpeg 参数中提取 `/artifacts` 的根地址。
    static func remoteArtifactBaseURL(from arguments: [String]) -> URL? {
        for argument in arguments {
            guard argument.contains("/artifacts/"),
                  let components = URLComponents(string: argument),
                  let path = components.percentEncodedPath.range(of: "/artifacts/"),
                  let scheme = components.scheme,
                  let host = components.host
            else {
                continue
            }
            var base = components
            let pathPrefix = String(components.percentEncodedPath[..<path.lowerBound])
            base.percentEncodedPath = pathPrefix + "/artifacts"
            base.percentEncodedQuery = nil
            base.fragment = nil
            guard !scheme.isEmpty, !host.isEmpty else { continue }
            return base.url
        }
        return nil
    }

    /// 停止代理并取消仍在接收或上传的请求。
    func stop() {
        lock.lock()
        guard !stopped else {
            lock.unlock()
            return
        }
        stopped = true
        let continuation = startContinuation
        startContinuation = nil
        let activeConnections = Array(connections.values)
        connections.removeAll()
        lock.unlock()

        listener.cancel()
        for connection in activeConnections {
            connection.stop()
        }
        // 共享会话随代理一起结束；finishTasksAndInvalidate 会等已发出的请求
        // 收尾，但此时 drain 已经跑过，剩下的都是该取消的。
        uploadSession.invalidateAndCancel()
        continuation?.resume(throwing: ProxyError.stopped)
        AppLogger.shared.info("任务上传代理已停止：job=\(jobID)")
    }

    /// 等待 ffmpeg 已经关闭的 PUT 请求完成，避免进程先退出、Worker 先 stop
    /// 代理，导致最后的 init/playlist 还在上传就被取消。
    func drainPendingUploads() async {
        let deadline = DispatchTime.now().uptimeNanoseconds + Self.uploadDrainTimeoutNanoseconds
        while !Task.isCancelled {
            if !hasPendingUploads() {
                await flushDeferredPlaylist()
                return
            }

            let now = DispatchTime.now().uptimeNanoseconds
            if now >= deadline {
                let message = "产物上传等待超时：job=\(jobID)"
                rememberFailure(message)
                AppLogger.shared.warning(message)
                return
            }
            let remaining = min(deadline - now, 20_000_000)
            do {
                try await Task.sleep(nanoseconds: remaining)
            } catch {
                return
            }
        }
    }

    /// 把攒下的最后一份 live.m3u8 补传给 NAS，供播放诊断查看。
    ///
    /// 它对播放不是必需品，所以失败只记日志、不写 failureMessage——否则一份
    /// 纯诊断产物没传上去，会把一个本来成功的任务判成失败。
    private func flushDeferredPlaylist() async {
        lock.lock()
        let pending = deferredPlaylist
        deferredPlaylist = nil
        lock.unlock()

        guard let pending else { return }
        // 直接走网络实传，不能再经过 upload()——那里会把它重新攒起来。
        let result = await performUpload(
            data: pending.data,
            filename: Self.deferredPlaylistName,
            query: pending.query
        )
        if !result.succeeded {
            AppLogger.shared.warning(
                "进度列表补传失败（不影响播放）：job=\(jobID) status=\(result.statusCode)"
            )
        }
    }

    /// 取一次性失败快照；失败不能被后续成功上传清掉，因为那份失败产物已经
    /// 可能让播放器读不到对应分片。
    var failureDescription: String? {
        lock.lock()
        defer { lock.unlock() }
        return failureMessage
    }

    private func rememberFailure(_ message: String) {
        lock.lock()
        if failureMessage == nil {
            failureMessage = message
        }
        lock.unlock()
    }

    private func hasPendingUploads() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return !connections.isEmpty
    }

    private func handleListenerState(_ state: NWListener.State) {
        switch state {
        case .ready:
            guard let port = listener.port else {
                failStart("系统未分配监听端口")
                return
            }
            lock.lock()
            started = true
            let continuation = startContinuation
            startContinuation = nil
            lock.unlock()
            guard let continuation else { return }
            let url = URL(string: "http://127.0.0.1:\(port)")!
            continuation.resume(returning: url)
            AppLogger.shared.info("任务启用内存上传代理：job=\(jobID) endpoint=127.0.0.1:\(port)")
        case let .failed(error):
            failStart(String(describing: error))
        case .cancelled:
            failStart(ProxyError.stopped.description)
        default:
            break
        }
    }

    private func failStart(_ message: String) {
        lock.lock()
        let continuation = startContinuation
        startContinuation = nil
        lock.unlock()
        continuation?.resume(throwing: ProxyError.listenerFailed(message))
    }

    private func accept(_ connection: NWConnection) {
        lock.lock()
        guard !stopped else {
            lock.unlock()
            connection.cancel()
            return
        }
        let id = ObjectIdentifier(connection)
        let handler = UploadConnection(id: id, connection: connection, proxy: self, queue: queue)
        connections[id] = handler
        lock.unlock()
        handler.start()
    }

    private func connectionDidFinish(_ id: ObjectIdentifier) {
        lock.lock()
        connections.removeValue(forKey: id)
        lock.unlock()
    }

    private func remoteURL(filename: String, query: String?) -> URL? {
        guard var components = URLComponents(
            url: remoteBaseURL,
            resolvingAgainstBaseURL: false
        ) else {
            return nil
        }
        let encodedFilename = filename.addingPercentEncoding(
            withAllowedCharacters: .urlPathAllowed
        ) ?? filename
        let basePath = components.percentEncodedPath
        components.percentEncodedPath = (basePath.hasSuffix("/") ? basePath : basePath + "/") + encodedFilename
        components.percentEncodedQuery = query
        components.fragment = nil
        return components.url
    }

    /// 代理收到产物后的入口：决定实传还是攒着，真正的网络请求在 performUpload。
    private func upload(
        data: Data,
        filename: String,
        query: String?
    ) async -> UploadResult {
        // VOD 进度列表只留最后一份，收尾时补传一次（理由见 deferredPlaylistName）。
        // 对 ffmpeg 直接回 201，它不会因此改变写入行为。
        if Self.isDeferredArtifact(filename) {
            lock.lock()
            deferredPlaylist = (data: data, query: query)
            lock.unlock()
            return UploadResult(statusCode: 201, message: nil, attempts: 1)
        }
        return await performUpload(data: data, filename: filename, query: query)
    }

    private func performUpload(
        data: Data,
        filename: String,
        query: String?
    ) async -> UploadResult {
        guard let remoteURL = remoteURL(filename: filename, query: query) else {
            let message = "NAS 产物地址无效"
            rememberFailure("产物上传失败：name=\(filename) reason=\(message)")
            return UploadResult(statusCode: 400, message: message, attempts: 1)
        }

        let session = uploadSession

        var lastStatus = 502
        var lastMessage = "无法连接 NAS"
        for attempt in 1...Self.maxUploadAttempts {
            do {
                var request = URLRequest(url: remoteURL)
                request.httpMethod = "PUT"
                request.cachePolicy = .reloadIgnoringLocalCacheData
                request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
                request.setValue(String(data.count), forHTTPHeaderField: "Content-Length")
                request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
                request.httpBody = data

                let (_, response) = try await session.data(for: request)
                guard let httpResponse = response as? HTTPURLResponse else {
                    lastStatus = 502
                    lastMessage = "NAS 返回了无效 HTTP 响应"
                    if attempt < Self.maxUploadAttempts {
                        await retryDelay(after: attempt, filename: filename, reason: lastMessage)
                        continue
                    }
                    break
                }
                lastStatus = httpResponse.statusCode
                if (200..<300).contains(httpResponse.statusCode) {
                    if attempt > 1 {
                        AppLogger.shared.info(
                            "产物上传重试成功：job=\(jobID) name=\(filename) bytes=\(data.count) attempts=\(attempt)"
                        )
                    }
                    return UploadResult(statusCode: httpResponse.statusCode, message: nil, attempts: attempt)
                }

                lastMessage = "NAS 返回 HTTP \(httpResponse.statusCode)"
                guard Self.retryableStatusCodes.contains(httpResponse.statusCode),
                      attempt < Self.maxUploadAttempts
                else {
                    break
                }
                await retryDelay(after: attempt, filename: filename, reason: lastMessage)
            } catch {
                if Task.isCancelled {
                    return UploadResult(statusCode: 499, message: "上传任务已取消", attempts: attempt)
                }
                lastStatus = 502
                lastMessage = LogSanitizer.redact(error.localizedDescription)
                if attempt < Self.maxUploadAttempts {
                    await retryDelay(after: attempt, filename: filename, reason: lastMessage)
                }
            }
        }

        let failure =
            "产物上传失败：name=\(filename) bytes=\(data.count) " +
            "attempts=\(Self.maxUploadAttempts) status=\(lastStatus) reason=\(lastMessage)"
        rememberFailure(failure)
        AppLogger.shared.error("\(failure) job=\(jobID)")
        return UploadResult(statusCode: lastStatus, message: lastMessage, attempts: Self.maxUploadAttempts)
    }

    private func retryDelay(after attempt: Int, filename: String, reason: String) async {
        let delay = UInt64(attempt) * 250_000_000
        AppLogger.shared.warning(
            "产物上传将重试：job=\(jobID) name=\(filename) next_attempt=\(attempt + 1)/\(Self.maxUploadAttempts) delay_ms=\(delay / 1_000_000) reason=\(reason)"
        )
        do {
            try await Task.sleep(nanoseconds: delay)
        } catch {
            // stop() 取消任务时，尽快返回给调用方即可。
        }
    }

    private final class UploadConnection: @unchecked Sendable {
        private enum BodyMode {
            case chunked
            case contentLength(Int)
            case untilConnectionClose
        }

        private let id: ObjectIdentifier
        private let connection: NWConnection
        private weak var proxy: ArtifactUploadProxy?
        /// Network 回调、停止和上传完成回调必须汇聚到同一个串行队列。
        /// 该对象被 URLSession 的异步 Task 持有，不能只依赖 NWConnection
        /// 的回调队列来保护 responseSent 等状态。
        private let queue: DispatchQueue
        private var headerBuffer = Data()
        private var body = Data()
        private var mode: BodyMode?
        private var chunkedParser: ChunkedBodyParser?
        private var headersParsed = false
        private var bodyComplete = false
        private var uploadStarted = false
        private var responseSent = false
        private var stopped = false
        private var filename: String?
        private var query: String?
        private var uploadTask: Task<Void, Never>?

        init(
            id: ObjectIdentifier,
            connection: NWConnection,
            proxy: ArtifactUploadProxy,
            queue: DispatchQueue
        ) {
            self.id = id
            self.connection = connection
            self.proxy = proxy
            self.queue = queue
        }

        func start() {
            connection.stateUpdateHandler = { [weak self] state in
                guard let self else { return }
                if case let .failed(error) = state, !self.stopped, !self.bodyComplete {
                    self.abort("回环连接失败：\(error)")
                }
            }
            connection.start(queue: queue)
            receive()
        }

        func stop() {
            queue.async { [weak self] in
                self?.stopOnQueue()
            }
        }

        private func stopOnQueue() {
            guard !stopped else { return }
            stopped = true
            uploadTask?.cancel()
            uploadTask = nil
            connection.cancel()
            proxy?.connectionDidFinish(id)
        }

        private func receive() {
            connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) {
                [weak self] data, _, isComplete, error in
                guard let self else { return }
                guard !self.stopped else { return }
                if let data, !data.isEmpty, !self.bodyComplete {
                    self.consume(data)
                }

                if self.responseSent {
                    return
                }
                if self.bodyComplete {
                    self.beginUpload()
                    return
                }
                if case .untilConnectionClose? = self.mode, isComplete {
                    self.bodyComplete = true
                    self.beginUpload()
                    return
                }
                if let error {
                    self.abort("接收产物时连接失败：\(error)")
                    return
                }
                if isComplete {
                    self.abort("客户端在产物接收完成前关闭连接")
                    return
                }
                self.receive()
            }
        }

        private func consume(_ data: Data) {
            if !headersParsed {
                headerBuffer.append(data)
                guard headerBuffer.count <= ArtifactUploadProxy.maxHeaderBytes else {
                    respond(status: 431, reason: "Request Header Fields Too Large")
                    return
                }
                guard let range = headerBuffer.range(of: ArtifactUploadProxy.headerTerminator) else {
                    return
                }
                let headerData = Data(headerBuffer[..<range.lowerBound])
                let remainder = Data(headerBuffer[range.upperBound...])
                headerBuffer.removeAll(keepingCapacity: false)
                guard parseHeaders(headerData) else { return }
                headersParsed = true
                if !remainder.isEmpty {
                    consumeBody(remainder)
                }
                return
            }
            consumeBody(data)
        }

        private func parseHeaders(_ data: Data) -> Bool {
            guard let text = String(data: data, encoding: .utf8) else {
                respond(status: 400, reason: "Bad Request")
                return false
            }
            let lines = text.components(separatedBy: "\r\n")
            guard let requestLine = lines.first else {
                respond(status: 400, reason: "Bad Request")
                return false
            }
            let requestParts = requestLine.split(separator: " ", maxSplits: 2)
            guard requestParts.count == 3, requestParts[0].uppercased() == "PUT" else {
                respond(status: 405, reason: "Method Not Allowed")
                return false
            }

            let target = String(requestParts[1])
            let targetString = target.hasPrefix("/") ? "http://127.0.0.1\(target)" : target
            guard let targetComponents = URLComponents(string: targetString),
                  let path = targetComponents.percentEncodedPath.removingPercentEncoding,
                  let rawFilename = path.split(separator: "/").last
            else {
                respond(status: 400, reason: "Bad Request")
                return false
            }
            let filename = String(rawFilename)
            let range = NSRange(filename.startIndex..<filename.endIndex, in: filename)
            guard ArtifactUploadProxy.artifactNamePattern.firstMatch(in: filename, options: [], range: range) != nil else {
                respond(status: 404, reason: "Not Found")
                return false
            }
            guard let proxy,
                  proxy.remoteURL(filename: filename, query: targetComponents.percentEncodedQuery) != nil
            else {
                respond(status: 400, reason: "Bad Request")
                return false
            }

            var parsedHeaders: [String: String] = [:]
            for line in lines.dropFirst() {
                guard let separator = line.firstIndex(of: ":") else { continue }
                let key = line[..<separator].trimmingCharacters(in: .whitespaces).lowercased()
                let value = line[line.index(after: separator)...]
                    .trimmingCharacters(in: .whitespaces)
                parsedHeaders[key] = value
            }
            if parsedHeaders["expect"]?.lowercased() == "100-continue" {
                sendRaw("HTTP/1.1 100 Continue\r\n\r\n")
            }

            self.filename = filename
            query = targetComponents.percentEncodedQuery
            if parsedHeaders["transfer-encoding"]?.lowercased().contains("chunked") == true {
                mode = .chunked
                chunkedParser = ChunkedBodyParser(
                    maxBodyBytes: ArtifactUploadProxy.maxArtifactBytes
                )
            } else if let lengthText = parsedHeaders["content-length"],
                      let length = Int(lengthText),
                      length >= 0,
                      length <= ArtifactUploadProxy.maxArtifactBytes
            {
                mode = .contentLength(length)
                if length == 0 { bodyComplete = true }
            } else if parsedHeaders["content-length"] != nil {
                respond(status: 413, reason: "Payload Too Large")
                return false
            } else {
                // 兼容没有 Content-Length 也没有 chunked 的 HTTP 客户端，
                // 以连接关闭作为 body 结束信号；ffmpeg 正常路径不会走这里。
                mode = .untilConnectionClose
            }
            return true
        }

        private func consumeBody(_ data: Data) {
            guard let mode else { return }
            switch mode {
            case let .contentLength(length):
                guard appendBody(data), body.count <= length else {
                    respond(status: 413, reason: "Payload Too Large")
                    return
                }
                if body.count == length { bodyComplete = true }
            case .untilConnectionClose:
                guard appendBody(data) else {
                    respond(status: 413, reason: "Payload Too Large")
                    return
                }
            case .chunked:
                guard var parser = chunkedParser else {
                    abort("chunked body 解析器未初始化")
                    return
                }
                let result = parser.append(data)
                chunkedParser = parser
                switch result {
                case .incomplete:
                    break
                case let .complete(parsedBody):
                    body = parsedBody
                    bodyComplete = true
                case let .invalid(message):
                    abort(message)
                case .tooLarge:
                    respond(status: 413, reason: "Payload Too Large")
                }
            }
        }

        private func appendBody(_ data: Data) -> Bool {
            guard data.count <= ArtifactUploadProxy.maxArtifactBytes - body.count else {
                return false
            }
            body.append(data)
            return true
        }

        private func beginUpload() {
            guard !stopped, !uploadStarted, !responseSent,
                  let filename,
                  let proxy
            else { return }
            uploadStarted = true
            let data = body
            let query = query
            uploadTask = Task { [weak self, weak proxy] in
                guard let self, let proxy else { return }
                let result = await proxy.upload(data: data, filename: filename, query: query)
                self.queue.async { [weak self] in
                    self?.finishUpload(result)
                }
            }
        }

        private func finishUpload(_ result: UploadResult) {
            guard !stopped, !responseSent else { return }
            if result.succeeded {
                respond(status: result.statusCode, reason: "Created")
            } else {
                respond(status: result.statusCode, reason: result.message ?? "Bad Gateway")
            }
        }

        private func abort(_ message: String) {
            guard !stopped, !responseSent else { return }
            let name = filename ?? "unknown"
            AppLogger.shared.warning(
                "本地上传代理收到不完整产物：job=\(proxy?.jobID ?? "unknown") name=\(name) received_bytes=\(body.count) reason=\(message)"
            )
            respond(status: 499, reason: "Client Closed Request")
        }

        private func respond(status: Int, reason: String) {
            guard !stopped, !responseSent else { return }
            responseSent = true
            let safeReason = reason.replacingOccurrences(of: "\r", with: " ")
                .replacingOccurrences(of: "\n", with: " ")
            if status >= 400 {
                proxy?.rememberFailure(
                    "产物上传代理拒绝：name=\(filename ?? "unknown") HTTP \(status) \(safeReason)"
                )
            }
            sendRaw(
                "HTTP/1.1 \(status) \(safeReason)\r\n" +
                "Content-Length: 0\r\nConnection: close\r\n\r\n"
            ) { [weak self] in
                self?.close()
            }
        }

        private func sendRaw(_ response: String, completion: (() -> Void)? = nil) {
            connection.send(
                content: Data(response.utf8),
                completion: .contentProcessed { _ in completion?() }
            )
        }

        private func close() {
            guard !stopped else { return }
            stopped = true
            uploadTask = nil
            connection.cancel()
            proxy?.connectionDidFinish(id)
        }
    }
}
