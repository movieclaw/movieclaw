import XCTest

@testable import MovieClawTranscoder

/// 产物上传代理的停机行为。
///
/// 这次的教训（2026-08-30）：用户一按快进，菜单栏 App 整个消失，自己的日志
/// 里一行都没有。崩溃报告（~/Library/Logs/DiagnosticReports）指向
/// `-[__NSURLSessionLocal taskForClassInfo:]` 抛的 Objective-C 异常——seek 时
/// 服务端下发 job.stop，`stop()` 把上传会话 invalidate，而旧任务的收尾路径
/// 还在 `drainPendingUploads()` 里往同一条会话上补传 live.m3u8。ObjC 异常在
/// Swift 里 catch 不住，进程当场 abort。
final class ArtifactUploadProxyTests: XCTestCase {
    /// 停机后再收尾，必须安静地什么都不做，而不是把整个进程带走。
    ///
    /// 这条用例在修复前会让测试进程直接 abort（不是 XCTFail，是崩）。
    func testDrainAfterStopDoesNotTouchInvalidatedSession() async throws {
        // 回传地址指向丢弃端口：本用例只关心停机后的行为，不需要真的传到 NAS。
        let proxy = try ArtifactUploadProxy(
            jobID: "test-stop-then-drain",
            remoteBaseURL: URL(string: "http://127.0.0.1:9/artifacts")!
        )
        let localBase = try await proxy.start()

        // 先让代理攒下一份 live.m3u8：它是延迟产物，只在收尾时补传一次。
        var request = URLRequest(url: localBase.appendingPathComponent("live.m3u8"))
        request.httpMethod = "PUT"
        request.httpBody = Data("#EXTM3U\n#EXT-X-ENDLIST\n".utf8)
        let (_, response) = try await URLSession(configuration: .ephemeral).data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 201)

        // seek 的真实顺序：先 stop（会话被 invalidate），收尾才姗姗来迟。
        proxy.stop()
        await proxy.drainPendingUploads()

        // 主动放弃的补传不算失败，否则一次正常的 seek 会被判成任务失败。
        XCTAssertNil(proxy.failureDescription)
    }
}
