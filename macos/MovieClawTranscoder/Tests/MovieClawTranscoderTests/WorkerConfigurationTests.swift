import XCTest
@testable import MovieClawTranscoder

final class WorkerConfigurationTests: XCTestCase {
    func testAllowsHTTPSAddress() throws {
        let configuration = try makeConfiguration(nasText: "https://mc.z.lk.mk/")

        XCTAssertEqual(configuration.nasURL.absoluteString, "https://mc.z.lk.mk")
        XCTAssertFalse(configuration.usesInsecureHTTP)
    }

    func testAllowsExplicitInternalHTTPAddress() throws {
        let configuration = try makeConfiguration(nasText: "HTTP://10.1.1.5:3000/")

        XCTAssertEqual(configuration.nasURL.absoluteString, "http://10.1.1.5:3000")
        XCTAssertTrue(configuration.usesInsecureHTTP)
    }

    func testRejectsUnsupportedScheme() {
        XCTAssertThrowsError(try makeConfiguration(nasText: "ftp://10.1.1.5:3000"))
    }

    func testRejectsQueryAndCredentials() {
        XCTAssertThrowsError(try makeConfiguration(nasText: "http://user:pass@10.1.1.5:3000"))
        XCTAssertThrowsError(try makeConfiguration(nasText: "http://10.1.1.5:3000?token=secret"))
    }

    func testArtifactProxyFindsBaseAndRewritesArtifactURLsOnly() throws {
        let remote = URL(string: "https://nas.example:3000/data/transcodes/job/artifacts")!
        let proxy = try ArtifactUploadProxy(jobID: "job", remoteBaseURL: remote)
        let local = URL(string: "http://127.0.0.1:43123")!
        let arguments = [
            "-i",
            "https://nas.example/source/movie.mkv?token=secret",
            "https://nas.example:3000/data/transcodes/job/artifacts/live.m3u8?token=secret",
            "https://nas.example:3000/data/transcodes/job/artifacts/seg%05d.m4s?token=secret",
            "https://nas.example:3000/data/transcodes/job/artifacts/index.m3u8?token=secret",
            "init.mp4?token=secret",
        ]

        XCTAssertEqual(
            ArtifactUploadProxy.remoteArtifactBaseURL(from: arguments),
            remote
        )
        XCTAssertEqual(
            proxy.rewrite(arguments: arguments, localBaseURL: local),
            [
                "-i",
                "https://nas.example/source/movie.mkv?token=secret",
                "http://127.0.0.1:43123/live.m3u8?token=secret",
                "http://127.0.0.1:43123/seg%05d.m4s?token=secret",
                "http://127.0.0.1:43123/index.m3u8?token=secret",
                "http://127.0.0.1:43123/init.mp4?token=secret",
            ]
        )
    }

    func testArtifactProxyLimitMatchesServerDefault() {
        XCTAssertEqual(ArtifactUploadProxy.maxArtifactBytes, 512 * 1024 * 1024)
    }

    func testOnlyVODProgressPlaylistIsDeferred() {
        // live.m3u8 是 VOD 的内部进度列表，服务端对远程会话根本不解析它，
        // 每个分片都重传一遍纯属浪费。
        XCTAssertTrue(ArtifactUploadProxy.isDeferredArtifact("live.m3u8"))

        // 其余产物都必须实时上传。index.m3u8 尤其不能延迟——非 VOD 会话
        // 的浏览器直接读它，服务端起播时还会阻塞等它出现。
        XCTAssertFalse(ArtifactUploadProxy.isDeferredArtifact("index.m3u8"))
        XCTAssertFalse(ArtifactUploadProxy.isDeferredArtifact("init.mp4"))
        XCTAssertFalse(ArtifactUploadProxy.isDeferredArtifact("seg00001.m4s"))
    }

    func testChunkedBodyParserHandlesEveryTCPSplitAndTrailers() {
        let expected = Data("Wikipedia".utf8)
        let encoded = Data(
            "4\r\nWiki\r\n5;extension=1\r\npedia\r\n0\r\nX-Test: ok\r\n\r\n".utf8
        )
        var parser = ChunkedBodyParser(maxBodyBytes: 1024)
        var result: ChunkedBodyParseResult = .incomplete

        // 一个字节一个字节喂入，覆盖块大小行、块体、CRLF 和 trailer 的所有
        // TCP 拆包位置；也会实际覆盖 Data.startIndex 已经不是 0 的情况。
        for byte in encoded {
            result = parser.append(Data([byte]))
        }

        XCTAssertEqual(result, .complete(expected))
    }

    func testChunkedBodyParserRejectsMissingChunkTerminator() {
        var parser = ChunkedBodyParser(maxBodyBytes: 1024)

        XCTAssertEqual(
            parser.append(Data("4\r\nWikiX\r\n".utf8)),
            .invalid("chunked body 缺少结束换行")
        )
    }

    // MARK: - 设备配对

    func testPairingParsesGrantFromEnvelope() async throws {
        let payload = #"""
        {"success":true,"code":"OK","message":"请在浏览器里核对配对码并批准",
         "data":{"user_code":"MCLW-7F3K","device_code":"dc-abc","interval":3,
                 "verification_uri":"http://10.1.1.5:3000/settings/devices","expires_in":300}}
        """#
        let pairing = DevicePairing(nasURL: URL(string: "http://10.1.1.5:3000")!, session: stubSession(200, payload))

        let grant = try await pairing.authorize(clientName: "Yi的Mac-mini")

        XCTAssertEqual(grant.userCode, "MCLW-7F3K")
        XCTAssertEqual(grant.deviceCode, "dc-abc")
        XCTAssertEqual(grant.interval, 3)
        XCTAssertEqual(grant.expiresIn, 300)
        XCTAssertEqual(grant.verificationURI, "http://10.1.1.5:3000/settings/devices")
    }

    func testPollMapsStatusCodesToDistinctOutcomes() async throws {
        // 四种结论必须泾渭分明：只有 pending 和 slowDown 该继续等，
        // finished 必须停止轮询，否则用户会看着一个永远转不完的圈。
        //
        // 结果先落到局部量再断言：XCTAssertEqual 收的是**非 async** 的
        // autoclosure，`try await` 塞不进去。
        let url = URL(string: "http://10.1.1.5:3000")!

        let pending = DevicePairing(nasURL: url, session: stubSession(202, #"{"data":null}"#))
        let pendingResult = try await pending.poll(deviceCode: "dc")
        XCTAssertEqual(pendingResult, .pending)

        let slow = DevicePairing(nasURL: url, session: stubSession(429, #"{"data":null}"#))
        let slowResult = try await slow.poll(deviceCode: "dc")
        XCTAssertEqual(slowResult, .slowDown)

        let granted = DevicePairing(
            nasURL: url,
            session: stubSession(200, #"{"data":{"token":"mclaw_abc","client_name":"Yi的Mac-mini"}}"#)
        )
        let grantedResult = try await granted.poll(deviceCode: "dc")
        XCTAssertEqual(grantedResult, .granted(token: "mclaw_abc", clientName: "Yi的Mac-mini"))

        let denied = DevicePairing(
            nasURL: url,
            session: stubSession(400, #"{"success":false,"message":"接入请求已被拒绝，请重新发起配对"}"#)
        )
        let deniedResult = try await denied.poll(deviceCode: "dc")
        XCTAssertEqual(deniedResult, .finished(reason: "接入请求已被拒绝，请重新发起配对"))
    }

    func testVerifyConnectionSurfacesServerMessage() async {
        // 服务端的中文 message 要原样透传：它比「HTTP 500」有用得多
        let pairing = DevicePairing(
            nasURL: URL(string: "http://10.1.1.5:3000")!,
            session: stubSession(503, #"{"success":false,"message":"服务正在启动，请稍候"}"#)
        )
        // 手写 do/catch 而不是 XCTAssertThrowsError：后者同样只收非 async 的
        // autoclosure。
        do {
            _ = try await pairing.verifyConnection()
            XCTFail("503 必须抛错，不能当成连接成功")
        } catch {
            XCTAssertEqual(error.localizedDescription, "服务正在启动，请稍候")
        }
    }

    // MARK: - 测试基建

    /// 把固定的状态码与响应体喂给 DevicePairing，不发真实网络请求。
    private func stubSession(_ status: Int, _ body: String) -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        StubURLProtocol.status = status
        StubURLProtocol.body = Data(body.utf8)
        return URLSession(configuration: configuration)
    }

    private func makeConfiguration(nasText: String) throws -> WorkerConfiguration {
        try WorkerConfiguration.make(
            nasText: nasText,
            token: "worker-token",
            workerID: "macmini-m1",
            ffmpegPath: "/opt/homebrew/bin/jellyfin-ffmpeg",
            maxJobs: 1
        )
    }
}

/// 返回固定响应的 URLProtocol，用于给 DevicePairing 喂假服务器。
final class StubURLProtocol: URLProtocol {
    nonisolated(unsafe) static var status = 200
    nonisolated(unsafe) static var body = Data()

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: Self.status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Self.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
