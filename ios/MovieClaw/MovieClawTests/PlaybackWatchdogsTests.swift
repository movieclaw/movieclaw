import Foundation
import Testing
@testable import MovieClaw

/// 播放器看门狗的纯逻辑：掉帧窗口、卡顿归因、拖动跟随节奏。阈值逐条对照 Web
/// `lib/player/framedrop.ts`、`lib/player/stall.ts`（含 engine.ts watchStall 的推一把）、`lib/player/scrub-follow.ts`。
struct PlaybackWatchdogsTests {
    @Test func frameDropNeedsFullWindowAndMinFrames() {
        var tracker = FrameDropTracker()
        // 前 10 个样本构不成 10 秒窗口
        for second in 0 ..< 10 {
            #expect(tracker.sample(dropped: second * 5, total: second * 24) == nil)
        }
        // 第 11 个：窗口 240 帧掉 50 帧 ≈ 20.8% ≥ 10%
        let ratio = tracker.sample(dropped: 50, total: 240)
        #expect(ratio != nil && ratio! >= FrameDropTracker.ratio)
    }

    @Test func frameDropIgnoresTinyWindowsAndResetsOnCounterDrop() {
        var tracker = FrameDropTracker()
        for second in 0 ... 10 {
            // 10 秒只解了 50 帧：不够 100 帧，不判
            #expect(tracker.sample(dropped: second, total: second * 5) == nil)
        }
        // 计数变小 = 换了流：窗口作废，从头攒
        #expect(tracker.sample(dropped: 0, total: 10) == nil)
    }

    @Test func stallDecodeStalledAfterNudges() {
        var watch = StallWatch()
        var verdicts: [StallWatch.Verdict] = []
        // 先正常播几秒（真正播起来过），然后卡住不动、前方缓冲 10 秒
        for second in 0 ..< 3 {
            verdicts.append(watch.sample(time: Double(second), bufferedAhead: 10, paused: false, ended: false, seeking: false, starveLimit: 45))
        }
        for _ in 0 ..< 20 {
            verdicts.append(watch.sample(time: 2, bufferedAhead: 10, paused: false, ended: false, seeking: false, starveLimit: 45))
        }
        let index = try! #require(verdicts.firstIndex(of: .decodeStalled))
        // 判死之前恰好推了两把；推满两把后 8 秒判死：3 + 3 + 8
        #expect(verdicts[..<index].filter { $0 == .nudge }.count == StallWatch.maxNudges)
        #expect(index - 2 == 3 + 3 + 8)
    }

    @Test func stallStarvedUsesDirectLimit() {
        var watch = StallWatch()
        var starvedAt: Int?
        for second in 1 ... 50 {
            if watch.sample(time: 0, bufferedAhead: 0.5, paused: false, ended: false, seeking: false, starveLimit: StallWatch.directStarveSeconds) == .starved {
                starvedAt = second
                break
            }
        }
        #expect(starvedAt == StallWatch.directStarveSeconds)
        #expect(StallWatch.reason(.starved, starveLimit: 15).hasPrefix("等待取流超过 15 秒"))
        #expect(StallWatch.reason(.starved, starveLimit: 45).hasPrefix("等待服务端供流超过 45 秒"))
    }

    @Test func stallIgnoresPausedAndSeeking() {
        var watch = StallWatch()
        for _ in 0 ..< 60 {
            #expect(watch.sample(time: 5, bufferedAhead: 0, paused: true, ended: false, seeking: false, starveLimit: 15) == .ok)
            #expect(watch.sample(time: 5, bufferedAhead: 0, paused: false, ended: false, seeking: true, starveLimit: 15) == .ok)
        }
    }

    @Test func scrubFollowPlans() {
        #expect(ScrubFollow.plan(nowMs: 1000, lastFollowMs: 0, cheap: true, reachable: false, settleOnly: false) == .skip)
        #expect(ScrubFollow.plan(nowMs: 1000, lastFollowMs: 0, cheap: false, reachable: true, settleOnly: false) == .skip)
        #expect(ScrubFollow.plan(nowMs: 1000, lastFollowMs: 0, cheap: false, reachable: true, settleOnly: true) == .deferred(ms: 60))
        #expect(ScrubFollow.plan(nowMs: 1000, lastFollowMs: 0, cheap: true, reachable: true, settleOnly: false) == .follow)
        // 上次跟随刚过 70ms：再等 30ms 就到 100ms 兜底
        #expect(ScrubFollow.plan(nowMs: 1070, lastFollowMs: 1000, cheap: true, reachable: true, settleOnly: false) == .deferred(ms: 30))
    }
}
