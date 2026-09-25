import Foundation

/// 掉帧看门狗（对应 Web `lib/player/framedrop.ts`，阈值逐一照搬）。
///
/// 决策层不预测「这台设备放不放得动」，由这里的**真实证据**回答：视频直通期间持续掉帧超阈值，
/// 就把当前档报废，走既有的 failed_tiers 降档回路换转码重来。
/// - 窗口 10 秒（每秒一个样本，所以要 11 个样本才构成 10 秒的首尾差）：瞬时掉帧（seek 落点、解码起步）几秒就摊平了；
/// - 窗口内至少 100 帧：挡住「刚起播 3 掉 1 就算 33%」这类样本不足的误判；
/// - 比率 ≥ 10%：肉眼已明显卡顿，降档的代价（整路转码 + 一次换流）才值得付。
///
/// 调用方契约（否则会算出假掉帧）：后台、暂停、seek、换会话时 `reset()`；只在视频直通（copy）时喂样本。
struct FrameDropTracker {
    static let windowSamples = 10
    static let minFrames = 100
    static let ratio = 0.1

    private var history: [(dropped: Int, total: Int)] = []

    /// 喂一个累计样本；返回 nil = 没到判定条件，否则是窗口掉帧率（≥ 阈值时应降档）
    mutating func sample(dropped: Int, total: Int) -> Double? {
        if let last = history.last, total < last.total || dropped < last.dropped {
            // 累计计数变小 = 引擎换了流，旧窗口作废（调用方漏 reset 的兜底）
            history.removeAll()
        }
        history.append((dropped, total))
        if history.count > Self.windowSamples + 1 { history.removeFirst() }
        guard history.count == Self.windowSamples + 1, let first = history.first else { return nil }
        let totalDelta = total - first.total
        guard totalDelta >= Self.minFrames else { return nil }
        return Double(dropped - first.dropped) / Double(totalDelta)
    }

    mutating func reset() { history.removeAll() }
}

/// 卡顿归因看门狗（对应 Web `lib/player/stall.ts` + `engine.ts` 的 watchStall，常量逐一照搬）。
///
/// 只看「播放头不动」会把两件处置完全相反的事混为一谈：
/// - **解码卡死**：缓冲里明明还有 ≥3 秒却不走——先原地推两把（AVPlayer 会在换流 + seek 后楞住，
///   微调一下就能踢活），推不动 8 秒判失败走降档；
/// - **缺粮**：前方缓冲见底，多半是追上了转码器，正常现象，给足 45 秒；
///   原文件直出没有转码器可等，十几秒一个字节不到只能是线路不够或断了，15 秒就判。
///
/// 每秒喂一次；暂停、结束、定位中、播放头前进都不算停顿。
struct StallWatch {
    static let decodeStallSeconds = 8
    static let decodeStallMinBuffer = 3.0
    static let starveSeconds = 45
    static let directStarveSeconds = 15
    static let nudgeAtSeconds = 3
    static let maxNudges = 2
    static let nudgeStep = 0.1

    enum Verdict: Equatable {
        case ok
        /// 推一把：跳到当前位置 + 0.1 秒重新触发解码管线
        case nudge
        case decodeStalled
        case starved
    }

    private var lastTime: Double?
    private var stalledFor = 0
    private var nudges = 0
    private var sinceNudge = 99
    private var everAdvanced = false

    mutating func reset() { self = StallWatch() }

    mutating func sample(time: Double, bufferedAhead: Double, paused: Bool, ended: Bool, seeking: Bool, starveLimit: Int) -> Verdict {
        let advanced = lastTime.map { time > $0 } ?? false
        // 「真正播起来过」只认小步前进：起播定位、用户拖动是一次大跳，不算
        if advanced, !seeking, let lastTime, time - lastTime < 5 { everAdvanced = true }
        lastTime = time
        sinceNudge += 1
        if paused || ended || seeking || advanced {
            stalledFor = 0
            // 只有远离上次推动的真实前进才算恢复——推动自己造成的播放头变化不作数
            if advanced, sinceNudge > 3 { nudges = 0 }
            return .ok
        }
        stalledFor += 1
        if bufferedAhead >= Self.decodeStallMinBuffer {
            if stalledFor >= Self.decodeStallSeconds {
                stalledFor = 0
                nudges = 0
                return .decodeStalled
            }
            // 有数据却不动：先推一把（起播预滚阶段不推，否则会把预滚冲掉重来）
            if everAdvanced, stalledFor >= Self.nudgeAtSeconds, nudges < Self.maxNudges {
                nudges += 1
                sinceNudge = 0
                stalledFor = 0
                return .nudge
            }
            return .ok
        }
        if stalledFor >= starveLimit {
            stalledFor = 0
            nudges = 0
            return .starved
        }
        return .ok
    }

    /// 判定 → 给用户看的中文原因（同 Web stallReason，「浏览器」换成「播放器」）
    static func reason(_ verdict: Verdict, starveLimit: Int) -> String {
        if verdict == .decodeStalled {
            return "播放停滞超过 \(decodeStallSeconds) 秒，这一档的码流播放器吃不下"
        }
        return starveLimit < starveSeconds
            ? "等待取流超过 \(starveLimit) 秒——线路装不下这部片的码率，或连接已中断"
            : "等待服务端供流超过 \(starveLimit) 秒——转码速度跟不上播放，或转码已中断"
    }
}

/// 拖动跟随的节奏（对应 Web `lib/player/scrub-follow.ts`）：后沿落地 + 连续扫动 10Hz 兜底。
///
/// 跳转便宜（落点已在缓冲里）时，拖动途中画面跟着手指走；原文件直出拖出缓冲时只在**手指停住**
/// 60ms 后跟一次（每次 seek 都是一条新的 Range 请求，扫动途中跟只会一路抽）；其余情况松手才跳。
enum ScrubFollow {
    static let settleMs = 60
    static let maxWaitMs = 100

    enum Plan: Equatable {
        case skip
        case follow
        case deferred(ms: Int)
    }

    static func plan(nowMs: Int, lastFollowMs: Int, cheap: Bool, reachable: Bool, settleOnly: Bool) -> Plan {
        guard reachable else { return .skip }
        guard cheap else { return settleOnly ? .deferred(ms: settleMs) : .skip }
        let waited = nowMs - lastFollowMs
        if waited >= maxWaitMs { return .follow }
        return .deferred(ms: max(0, min(settleMs, maxWaitMs - waited)))
    }
}
