import Testing
@testable import MovieClaw

/// 分享访客的本机续播点：口径同服务端 resolve_progress（看过 90% 或到片尾记 0）
@MainActor
struct PlaybackProgressTests {
    @Test func localResumeKeepsMiddlePosition() {
        #expect(PlaybackAPI.localResume(1_800_000, durationMs: 6_000_000) == 1_800_000)
    }

    @Test func localResumeClearsNearEnd() {
        // 播完时控制器吸附到片长；看过 90% 也算看完——下次从头放，而不是停在片尾
        #expect(PlaybackAPI.localResume(6_000_000, durationMs: 6_000_000) == 0)
        #expect(PlaybackAPI.localResume(5_400_000, durationMs: 6_000_000) == 0)
        #expect(PlaybackAPI.localResume(5_399_000, durationMs: 6_000_000) == 5_399_000)
    }

    @Test func localResumeWithoutDurationKeepsPosition() {
        #expect(PlaybackAPI.localResume(120_000, durationMs: nil) == 120_000)
        #expect(PlaybackAPI.localResume(nil, durationMs: 6_000_000) == nil)
    }
}
