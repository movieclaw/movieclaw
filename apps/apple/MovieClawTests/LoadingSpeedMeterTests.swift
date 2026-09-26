import Foundation
import Testing
@testable import MovieClaw

/// 顶栏「↓」实时加载速度计的纯逻辑（口径见 PlayerEngine.swift 的 LoadingSpeedMeter）：
/// 在下载报实际速度、没在下载报 0、日志没建报 nil、起播第一坨按传输时长折算、计数攒一坨再记时按 2 秒窗口摊平。
struct LoadingSpeedMeterTests {
    private static let mb: Int64 = 1_048_576
    /// 1 MB/s 折成 bps
    private static let oneMBps = Double(mb) * 8

    @Test func noAccessLogMeansNoReading() {
        var meter = LoadingSpeedMeter()
        // 第一片还没下完、日志没建：这时其实在下，不能报 0
        #expect(meter.sample(bytes: nil, transferSeconds: 0, at: 0) == nil)
        #expect(meter.sample(bytes: nil, transferSeconds: 0, at: 1) == nil)
    }

    @Test func firstChunkUsesTransferDuration() {
        var meter = LoadingSpeedMeter()
        // HLS 第一片 2MB 在路上花了 2 秒，随日志一次性出现：报 1MB/s，不是按 1 秒采样间隔算出的 2MB/s
        let bps = meter.sample(bytes: 2 * Self.mb, transferSeconds: 2, at: 10)
        #expect(bps == Self.oneMBps)
    }

    @Test func firstChunkWithoutTransferDurationWaitsForNextSample() throws {
        var meter = LoadingSpeedMeter()
        // 原文件直出的访问日志不记传输时长：第一个点只当起点
        #expect(meter.sample(bytes: 200_000, transferSeconds: 0, at: 0) == nil)
        // 0.5 秒后还不够一个窗口：沿用（仍为空）
        #expect(meter.sample(bytes: 700_000, transferSeconds: 0, at: 0.5) == nil)
        // 满 1 秒给第一个读数
        let bpsReading = meter.sample(bytes: 200_000 + Self.mb, transferSeconds: 0, at: 1)
        let bps = try #require(bpsReading)
        #expect(abs(bps - Self.oneMBps) < 1)
    }

    @Test func steadyDownloadThenIdleDropsToZero() throws {
        var meter = LoadingSpeedMeter()
        _ = meter.sample(bytes: 0, transferSeconds: 0, at: 0)
        // 每秒 1MB 稳定下载
        for second in 1 ... 5 {
            let bpsReading = meter.sample(bytes: Int64(second) * Self.mb, transferSeconds: 0, at: Double(second))
            let bps = try #require(bpsReading)
            #expect(abs(bps - Self.oneMBps) < 1)
        }
        // 缓冲满了停下：第一秒窗口里还有一半在下，满 2 秒后归零
        let halfReading = meter.sample(bytes: 5 * Self.mb, transferSeconds: 0, at: 6)
        let half = try #require(halfReading)
        #expect(abs(half - Self.oneMBps / 2) < 1)
        #expect(meter.sample(bytes: 5 * Self.mb, transferSeconds: 0, at: 7) == 0)
        #expect(meter.sample(bytes: 5 * Self.mb, transferSeconds: 0, at: 30) == 0)
    }

    @Test func burstyCounterIsSmoothedOverTwoSeconds() throws {
        var meter = LoadingSpeedMeter()
        _ = meter.sample(bytes: 0, transferSeconds: 0, at: 0)
        _ = meter.sample(bytes: Self.mb, transferSeconds: 0, at: 1)
        _ = meter.sample(bytes: 2 * Self.mb, transferSeconds: 0, at: 2)
        // 实际一直是 1MB/s，但计数这一秒只记了 0.2MB、下一秒补记 1.8MB：2 秒窗口两次都读 1MB/s
        let firstReading = meter.sample(bytes: 2 * Self.mb + Self.mb / 5, transferSeconds: 0, at: 3)
        let first = try #require(firstReading)
        let secondReading = meter.sample(bytes: 4 * Self.mb, transferSeconds: 0, at: 4)
        let second = try #require(secondReading)
        #expect(abs(first - Self.oneMBps * 0.6) < 10)
        #expect(abs(second - Self.oneMBps) < 1)
    }

    @Test func denserCallsKeepTheWindowByTimeNotByCount() {
        var meter = LoadingSpeedMeter()
        _ = meter.sample(bytes: 0, transferSeconds: 0, at: 0)
        // 诊断面板开着时每秒会被读两次：窗口按时间算，读数不因调用次数变化
        var bps: Double?
        for step in 1 ... 10 {
            let t = Double(step) * 0.5
            bps = meter.sample(bytes: Int64(t * Double(Self.mb)), transferSeconds: 0, at: t)
        }
        #expect(abs((bps ?? 0) - Self.oneMBps) < 1)
    }

    @Test func counterGoingBackwardsStartsOver() throws {
        var meter = LoadingSpeedMeter()
        _ = meter.sample(bytes: 0, transferSeconds: 0, at: 0)
        _ = meter.sample(bytes: 10 * Self.mb, transferSeconds: 0, at: 1)
        // 换了播放项，计数从小数重新开始：不许出现负速度
        #expect(meter.sample(bytes: 100_000, transferSeconds: 0, at: 2) == nil)
        let bpsReading = meter.sample(bytes: 100_000 + Self.mb, transferSeconds: 0, at: 3)
        let bps = try #require(bpsReading)
        #expect(abs(bps - Self.oneMBps) < 1)
    }

    @Test func labelShowsZeroWhenIdleAndHidesWithoutReading() {
        #expect(PlaybackController.formatLoadingSpeed(nil) == nil)
        #expect(PlaybackController.formatLoadingSpeed(0) == "0 KB/s")
        #expect(PlaybackController.formatLoadingSpeed(Self.oneMBps * 3.2) == "3.2 MB/s")
        #expect(PlaybackController.formatLoadingSpeed(512 * 1024 * 8) == "512 KB/s")
    }
}
