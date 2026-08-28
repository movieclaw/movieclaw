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

    // MARK: - 配对码

    func testPairingCodeRoundTripsURLAndToken() throws {
        let code = try PairingCode.parse(makePairingCode(
            url: "https://nas.example.com",
            token: "s3cret-token"
        ))

        XCTAssertEqual(code.nasURL, "https://nas.example.com")
        XCTAssertEqual(code.token, "s3cret-token")
    }

    func testPairingCodeToleratesPastedWhitespaceAndNewlines() throws {
        // 从聊天工具或邮件里复制回来常常带换行和空格
        let raw = makePairingCode(url: "http://10.1.1.5:3000", token: "abc123")
        let mangled = "  " + raw.prefix(20) + "\n  " + raw.dropFirst(20) + "\n"

        let code = try PairingCode.parse(String(mangled))

        XCTAssertEqual(code.nasURL, "http://10.1.1.5:3000")
        XCTAssertEqual(code.token, "abc123")
    }

    func testPairingCodeRejectsForeignTextWithActionableMessage() {
        // 用户很可能误粘贴成 NAS 地址本身
        XCTAssertThrowsError(try PairingCode.parse("https://nas.example.com")) { error in
            XCTAssertEqual(error as? PairingCode.ParseError, .badPrefix)
        }
        XCTAssertThrowsError(try PairingCode.parse("   ")) { error in
            XCTAssertEqual(error as? PairingCode.ParseError, .empty)
        }
    }

    func testPairingCodeRejectsTruncatedPayload() {
        let raw = makePairingCode(url: "https://nas.example.com", token: "s3cret")
        // 少复制了尾巴：必须报「损坏」，不能静默解析出半个配置
        let truncated = String(raw.dropLast(6))

        XCTAssertThrowsError(try PairingCode.parse(truncated))
    }

    func testPairingCodeRejectsMissingToken() {
        let payload = #"{"url":"https://nas.example.com"}"#
        let encoded = Data(payload.utf8).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")

        XCTAssertThrowsError(try PairingCode.parse(PairingCode.prefix + encoded)) { error in
            XCTAssertEqual(error as? PairingCode.ParseError, .missingFields)
        }
    }

    /// 按网页端的生成规则拼一段配对码，用来验证两端格式一致。
    private func makePairingCode(url: String, token: String) -> String {
        let payload = try! JSONSerialization.data(
            withJSONObject: ["url": url, "token": token]
        )
        let encoded = payload.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        return PairingCode.prefix + encoded
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
