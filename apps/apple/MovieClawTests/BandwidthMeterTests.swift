import Foundation
import Testing
@testable import MovieClaw

/// 诊断面板「带宽」的纯逻辑（口径见 PlayerEngine.swift 的 BandwidthMeter）：
/// 最近 12 秒最快一次传输的速度、停下来不取时保持实测值、不会比加载速度小。门槛与 Web bandwidth.ts 一致。
struct BandwidthMeterTests {
    private static let mb: Double = 1_048_576
    /// 1 MB/s 折成 bps
    private static let oneMBps = mb * 8

    @Test func rateIsBytesOverTransferTimeNotWallClock() {
        var meter = BandwidthMeter()
        // 一个 1MB 的分片花 1 秒传完，然后 10 秒什么都没下（缓冲满了）：读数照旧是 1MB/s，不会掉向 0
        meter.push(bytes: Self.mb, transfer: 1, at: 0)
        #expect(meter.bps == Self.oneMBps)
    }

    @Test func tooFewBytesGiveNoReading() {
        var meter = BandwidthMeter()
        // init 分片才几 KB：宁可不显示也不显示错的
        meter.push(bytes: 8 * 1024, transfer: 0.01, at: 0)
        #expect(meter.bps == nil)
    }

    @Test func oldSamplesAreDroppedOnlyWhenNewOnesArrive() {
        var meter = BandwidthMeter()
        // 窗口外的老样本（2MB/s）被新样本挤掉：读数跟着线路降下来
        meter.push(bytes: Self.mb, transfer: 0.5, at: 0)
        for second in 1 ... 3 {
            meter.push(bytes: Self.mb, transfer: 1, at: BandwidthMeter.window + Double(second))
        }
        #expect(meter.bps == Self.oneMBps)
    }

    @Test func sparseSegmentsKeepTheLastThreeEvenOutsideTheWindow() {
        var meter = BandwidthMeter()
        // 转码会话隔十几秒才到一片，最新一片又被播放器读慢了：仍按最近三片里最快的算，不掉到 0.11
        meter.push(bytes: 1.8 * Self.mb, transfer: 1.8, at: 0)
        meter.push(bytes: 1.6 * Self.mb, transfer: 14, at: 17)
        #expect(meter.bps == Self.oneMBps)
        meter.push(bytes: 1.6 * Self.mb, transfer: 8.3, at: 31)
        #expect(meter.bps == Self.oneMBps)
    }

    @Test func slowTransfersHeldBackByThePlayerDoNotDragItDown() {
        var meter = BandwidthMeter()
        // 实测过的一组分片：线路 1MB/s，个别片被播放器自己放慢到三成、五成——带宽仍是 1MB/s，不是平均的 0.7
        for (index, seconds) in [1.0, 3.4, 2.0, 1.03].enumerated() {
            meter.push(bytes: Self.mb, transfer: seconds, at: Double(index) * 2)
        }
        #expect(meter.bps == Self.oneMBps)
    }

    @Test func invalidSamplesAreIgnored() {
        var meter = BandwidthMeter()
        meter.push(bytes: Self.mb, transfer: 0, at: 0)
        meter.push(bytes: 0, transfer: 1, at: 1)
        meter.push(bps: 0, at: 2)
        meter.push(bps: .infinity, at: 3)
        #expect(meter.bps == nil)
    }

    @Test func loadingReadingsFeedTheSameMeterAndBoundItFromBelow() {
        var meter = BandwidthMeter()
        // 原文件 / MPV 的样本是每秒一个的加载速度读数：下载开头结尾那格偏低不影响，带宽永远不小于刚读到的加载速度
        for (second, ratio) in [0.4, 1.0, 1.02, 0.97, 0.3].enumerated() {
            meter.push(bps: Self.oneMBps * ratio, at: Double(second))
            #expect((meter.bps ?? 0) >= Self.oneMBps * ratio)
        }
        #expect(meter.bps == Self.oneMBps * 1.02)
    }
}
