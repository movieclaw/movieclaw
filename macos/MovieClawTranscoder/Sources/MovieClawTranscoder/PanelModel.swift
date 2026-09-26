import Foundation

/// 状态面板要显示的全部内容。纯数据：由 ``make(status:configured:nasAddress:today:recent:now:)``
/// 从 Worker 状态与本地任务记录推出来，视图只管照着画，便于单测覆盖各种组合。
///
/// 面板按「现在最该看什么」分三种主体（``Body``）：
/// - 在转码：一张张任务卡片，主角是「看片的人现在流不流畅」（``JobCard/Health``），
///   而不是倍速、曲线这些只有懂转码的人才读得懂的数字——数字只留在悬停提示里；
/// - 空闲：今天的统计 + 最近结束的几条，不留一块空白；
/// - 其它（未配对、连接中、重连、出错、已断开）：一张提示卡片，把能做的动作直接放在卡片里，
///   不用再去菜单里找。
struct PanelModel: Equatable {
    struct JobCard: Equatable {
        var id: String
        var title: String
        /// 悬停提示：完整源文件名 + 技术细节（实时速度、片内位置），给想深究的人看。
        var tooltip: String?
        /// 转码方式，如「用硬件转成 H.264」。
        var encoder: String?
        /// 「看到 25:10 / 1:52:10」（NAS 推了观众位置时）或「已准备到 25:40」（没推时的退路）。
        var progress: String?
        /// 进度条：观众看到的比例、转码准备到的比例（0…1）。NAS 下发了片长才有，
        /// 没有时不画进度条。两段叠在一起，就是视频播放器里「已播放 / 已缓冲」那种条。
        var watchedFraction: Double?
        var preparedFraction: Double?
        var health: Health
        /// 卡片最下面那句人话。
        var explanation: String
    }

    /// 看片的人现在播得顺不顺——任务卡片要回答的唯一问题。
    ///
    /// 线索有三条：
    /// - NAS 暂停了任务（job.pause）⇒ 转码已经领先播放足够多，**这是最健康的状态**，
    ///   不能写成「暂停中」让人以为出了问题；
    /// - 在跑时的实时转码速度（扣掉暂停的时间）⇒ 比播放快就流畅，比播放慢就会卡。
    ///   ffmpeg 自己报的 speed 是「从起转到现在」的平均值，被 NAS 暂停过就会被拉低到
    ///   1 倍出头，看着像 Mac 很慢——所以不用它，用 MenuBarController 算的实时值；
    /// - NAS 推来的观众位置（`job.playback`）⇒ 观众暂停了、领先了多少秒。
    enum Health: Equatable {
        /// 刚起转，还没有进度。
        case starting
        /// 观众按了暂停。
        case viewerPaused
        /// NAS 让它歇着：已经领先播放一段。
        case ahead
        /// 转得比播放快不少。
        case smooth(speed: Double)
        /// 刚够跟上播放，Mac 再忙一点就可能缓冲。
        case tight(speed: Double)
        /// 比播放慢，观众会遇到缓冲。
        case lagging(speed: Double)

        /// 实时速度到多少算「流畅」、低于多少算「跟不上」（相对播放的倍数）。
        static let smoothThreshold = 1.3
        static let laggingThreshold = 1.0

        init(speed: Double) {
            if speed >= Self.smoothThreshold {
                self = .smooth(speed: speed)
            } else if speed >= Self.laggingThreshold {
                self = .tight(speed: speed)
            } else {
                self = .lagging(speed: speed)
            }
        }

        /// 卡片上带颜色的那几个字。
        var title: String {
            switch self {
            case .starting: return "正在准备画面"
            case .viewerPaused: return "观众暂停了"
            case .ahead: return "已提前准备好"
            case .smooth: return "播放流畅"
            case .tight: return "刚好跟得上"
            case .lagging: return "转码跟不上"
            }
        }

        var tone: Tone {
            switch self {
            case .starting, .viewerPaused, .ahead, .smooth: return .neutral
            case .tight: return .warning
            case .lagging: return .critical
            }
        }

        /// 「3.4 倍」：一位小数，整数时不带「.0」。
        static func times(_ speed: Double) -> String {
            let rounded = (speed * 10).rounded() / 10
            return rounded == rounded.rounded()
                ? "\(Int(rounded)) 倍"
                : String(format: "%.1f 倍", rounded)
        }
    }

    struct Summary: Equatable {
        /// 今天转了几次（一次播放算一次，拖进度条的重启合并在内）。
        var count: Int
        /// 转出的片长，如「4.2 小时」「38 分钟」。
        var media: String
        /// 失败了几次。
        var failures: Int
    }

    struct HistoryRow: Equatable {
        var title: String
        var outcome: JobRecord.Outcome
        /// 右侧的一句：「看到 25:10 · 12 分钟前」「整部转完 · 1 小时前」「失败 · 刚刚」
        var detail: String
        var tooltip: String?
    }

    enum Tone: Equatable {
        case neutral
        case warning
        case critical
    }

    /// 提示卡片里能直接点的动作。
    enum Action: Equatable {
        case connect
        /// 转码内核反复崩溃、已停止自动重启后，用户手动再试一次（同 connect）。
        case retry
        case reconnect
        case openSettings
        /// 授权失效：去设置里「断开并重新配置」重新走一遍配对。
        case pairAgain
        case copyDiagnostics
        case openLog

        var title: String {
            switch self {
            case .connect: return "连接"
            case .retry: return "重试"
            case .reconnect: return "立即重连"
            case .openSettings: return "打开设置"
            case .pairAgain: return "重新配对"
            case .copyDiagnostics: return "复制诊断"
            case .openLog: return "查看日志"
            }
        }
    }

    struct Notice: Equatable {
        var symbol: String
        var title: String
        var detail: String?
        var tone: Tone
        var actions: [Action]
        /// 标题旁转一个小圈：连接中这类「马上就有结论」的状态。
        var spinning = false
    }

    enum Body: Equatable {
        case jobs([JobCard])
        case idle(Summary?, [HistoryRow])
        case notice(Notice)
    }

    /// CPU / 内存小图表（iStat Menus 那种）。见 ``ResourceMonitor``。
    struct Resources: Equatable {
        struct Gauge: Equatable {
            /// 右上角的大数字：「18%」「420 MB」
            var value: String
            /// 图表下面的对照：「整机 34%」「整机 12.2 / 16 GB」
            var detail: String
            /// 转码这一层（0…1，最近 2 分钟，新的在后）
            var series: [Double]
            /// 垫在底下的整机那一层（只有 CPU 有）
            var background: [Double]?
            var tooltip: String
        }

        var cpu: Gauge
        var memory: Gauge
    }

    /// 采样器的数据，原样传进来算展示内容（纯数据，便于单测）。
    struct ResourceHistory: Equatable {
        var samples: [ResourceMonitor.Sample]
        var physicalMemory: UInt64
        var cores: Int
    }

    var presentation: WorkerStatePresentation
    var subtitle: String
    var body: Body
    /// 没连上（未配对、刚启动还没采到数据）时为 nil，不显示。
    var resources: Resources?
    /// 面板底部的一行能力说明（ffmpeg 版本与硬件编码器）。
    var footnote: String?
    /// 「今天自动恢复过 2 次 · 最近 23:10 被信号 11 终止」——内核出过问题、App 自己兜住了。
    /// 让用户知道发生过、不用管；一次都没有时为 nil，不显示。
    var recoveryNote: String?
    var badge: MenuBarIcon.Badge

    /// 面板「最近」列表的条数。
    static let recentLimit = 4

    static func make(
        status: WorkerStatus?,
        configured: Bool,
        nasAddress: String?,
        today: [JobRecord],
        recent: [JobRecord],
        speeds: [String: Double] = [:],
        resources: ResourceHistory? = nil,
        recoveries: [CoreRecovery] = [],
        now: Date = Date()
    ) -> PanelModel {
        let presentation = WorkerStatePresentation.make(status?.state, configured: configured)
        let host = DisplayText.host(of: nasAddress)

        guard let status else {
            let notice = configured
                ? Notice(symbol: "bolt.slash", title: "没有连接", detail: "连接后开始接收转码任务。",
                         tone: .neutral, actions: [.connect])
                : Notice(symbol: "link", title: "还没有配对",
                         detail: "打开设置，找到你的 movieclaw 并在网页上批准这台 Mac。",
                         tone: .neutral, actions: [.openSettings])
            return PanelModel(
                presentation: presentation,
                subtitle: configured
                    ? (host.map { "已配对 · \($0)" } ?? "已配对")
                    : "还没有连接任何 movieclaw",
                body: .notice(notice),
                resources: nil,
                footnote: nil,
                recoveryNote: nil,
                badge: .none
            )
        }

        let error = status.lastError.flatMap { $0.isEmpty ? nil : $0 }
        let idle = Body.idle(summary(today), history(recent, now: now))
        var body: Body
        var badge = MenuBarIcon.Badge.none

        switch status.state {
        case .ready:
            body = idle
        case .busy, .paused, .draining:
            // draining 只在更新 ffmpeg 前出现（等手上的任务转完再换），照常显示任务
            body = status.jobs.isEmpty ? idle : .jobs(status.jobs.map { jobCard($0, speed: speeds[$0.id]) })
            badge = status.jobs.isEmpty ? .none : .busy
        case .starting, .connecting:
            body = .notice(Notice(
                symbol: "antenna.radiowaves.left.and.right",
                title: status.message.isEmpty ? "正在连接 NAS" : status.message,
                detail: host.map { "movieclaw：\($0)" },
                tone: .neutral,
                actions: [],
                spinning: true
            ))
        case .reconnecting:
            // 断线时 message 先是错误原因、随后是「N 秒后重连」；两句都有用，但别重复
            let lines = [error, status.message == error ? nil : status.message].compactMap { $0 }
            body = .notice(Notice(
                symbol: "arrow.triangle.2.circlepath",
                title: "正在重连 NAS",
                detail: lines.isEmpty ? nil : lines.joined(separator: "\n"),
                tone: .warning,
                actions: [.reconnect, .copyDiagnostics]
            ))
            badge = .attention
        case .error:
            body = .notice(Notice(
                symbol: "exclamationmark.triangle.fill",
                title: "出错了",
                detail: error ?? status.message,
                tone: .critical,
                actions: [.reconnect, .openSettings, .openLog]
            ))
            badge = .attention
        case .stopped, .unconfigured:
            body = .notice(Notice(
                symbol: "bolt.slash",
                title: "已断开",
                detail: "连接后重新开始接收转码任务。",
                tone: .neutral,
                actions: [.connect]
            ))
        }

        // 需要专门说明的故障优先于上面按连接状态给的通用说法
        if let problem = status.problem {
            body = .notice(problemNotice(problem, status: status, error: error))
            badge = .attention
        }

        return PanelModel(
            presentation: presentation,
            subtitle: [status.workerID, host].compactMap { $0 }.joined(separator: " · "),
            body: body,
            resources: resources.flatMap(Self.resources),
            footnote: footnote(status),
            recoveryNote: recoveryNote(recoveries, now: now),
            badge: badge
        )
    }

    /// 每种故障一张提示卡片：说清发生了什么、App 在做什么、用户要不要做点什么。
    private static func problemNotice(_ problem: WorkerProblem, status: WorkerStatus, error: String?) -> Notice {
        switch problem {
        case .authRejected:
            return Notice(
                symbol: "lock.slash",
                title: "这台 Mac 的授权已失效",
                detail: (error ?? status.message) + "\n重新配对后会立刻连上；在那之前每 5 分钟自动再试一次。",
                tone: .critical,
                actions: [.pairAgain, .openLog]
            )
        case .remoteDisabled:
            return Notice(
                symbol: "pause.circle",
                title: "服务端还没打开远程转码",
                detail: "在 movieclaw 网页「应用 → 远程转码」打开开关后会自动连上（每分钟重试一次）。",
                tone: .warning,
                actions: [.reconnect]
            )
        case let .cooldown(until, failures):
            let last = error.map { "\n最近一次：\($0)" } ?? ""
            return Notice(
                symbol: "stethoscope",
                title: "暂停接单，正在自检",
                detail: "5 分钟内连续 \(failures) 个任务刚开始就失败，先停止接单并检查 ffmpeg，"
                    + "\(DisplayText.timeOfDay(until)) 自动恢复。\(last)",
                tone: .warning,
                actions: [.copyDiagnostics, .openLog]
            )
        case .coreRecovering:
            return Notice(
                symbol: "arrow.clockwise",
                title: "转码内核异常退出，正在自动恢复",
                detail: status.message + "\n菜单栏 App 不受影响，恢复后会自动重新连上 NAS。",
                tone: .warning,
                actions: [],
                spinning: true
            )
        case .coreCrashLoop:
            return Notice(
                symbol: "xmark.octagon",
                title: "转码内核反复崩溃，已停止自动重启",
                detail: status.message + "\n可能是 ffmpeg 或系统环境出了问题；把诊断信息发给开发者能帮忙定位。",
                tone: .critical,
                actions: [.retry, .copyDiagnostics, .openLog]
            )
        case .ffmpegUnusable:
            return Notice(
                symbol: "shippingbox",
                title: "ffmpeg 不可用",
                detail: error ?? status.message,
                tone: .critical,
                actions: [.openSettings, .openLog]
            )
        }
    }

    private static func recoveryNote(_ recoveries: [CoreRecovery], now: Date) -> String? {
        let recent = recoveries.filter { now.timeIntervalSince($0.at) < 86_400 }
        guard let last = recent.last else { return nil }
        return "24 小时内自动恢复过 \(recent.count) 次 · 最近 \(DisplayText.timeOfDay(last.at)) \(last.reason)"
    }

    // MARK: - 各部分

    /// - Parameter speed: MenuBarController 算的实时速度（扣掉暂停）。刚起转、还没攒够
    ///   两次采样时为 nil，这时退回 ffmpeg 自己报的平均速度——起转阶段没暂停过，两者一样。
    private static func jobCard(_ job: RunningJob, speed: Double?) -> JobCard {
        let name = job.name.flatMap { $0.isEmpty ? nil : $0 }
        let transcoded = job.progress?.outTimeMS
        let playback = job.playback
        let watched = playback?.positionMS
        // 准备到片子的哪个位置：优先用 NAS 按分片算的（最准）；没有就用「这一轮的起点
        // + ffmpeg 转了多少」——ffmpeg 的进度从每次拖动后的起点重新数，直接用会出现
        // 观众在 1:12:00、这里却写「已准备到 0:45」
        let prepared = playback?.preparedMS ?? transcoded.map { job.startOffsetMS + $0 }
        let lead = watched.flatMap { watched in prepared.map { max(0, $0 - watched) } }

        let health: Health
        if transcoded == nil {
            health = .starting
        } else if playback?.viewerPaused == true {
            health = .viewerPaused
        } else if job.paused {
            health = .ahead
        } else if let live = speed ?? DisplayText.speedValue(job.progress?.speed) {
            health = Health(speed: live)
        } else {
            health = .starting
        }

        var progress: String?
        if let watched {
            progress = "看到 \(DisplayText.clock(milliseconds: watched))"
            if let duration = playback?.durationMS {
                progress! += " / \(DisplayText.clock(milliseconds: duration))"
            }
        } else if let prepared {
            progress = "已准备到 \(DisplayText.clock(milliseconds: prepared))"
        }
        var watchedFraction: Double?
        var preparedFraction: Double?
        if let duration = playback?.durationMS, duration > 0 {
            watchedFraction = watched.map { min(1, Double($0) / Double(duration)) }
            preparedFraction = prepared.map { min(1, Double($0) / Double(duration)) }
        }

        var details: [String] = []
        if let name { details.append(name) }
        if let live = speed, !job.paused {
            details.append(String(format: "实时转码速度 %.1f 倍（不含暂停）", live))
        }
        if let prepared {
            details.append("已准备到片中 \(DisplayText.clock(milliseconds: prepared))")
        }
        return JobCard(
            id: job.id,
            title: name.map(DisplayText.withoutExtension) ?? "任务 \(job.id.prefix(8))",
            tooltip: details.isEmpty ? nil : details.joined(separator: "\n"),
            encoder: DisplayText.encoder(job.videoEncoder),
            progress: progress,
            watchedFraction: watchedFraction,
            preparedFraction: preparedFraction,
            health: health,
            explanation: explanation(health, lead: lead, prepared: prepared)
        )
    }

    /// 卡片最下面那句人话：现在是什么情况、要不要做点什么。知道观众位置时说出真实的领先量。
    private static func explanation(_ health: Health, lead: Int64?, prepared: Int64?) -> String {
        let leadText = lead.flatMap { $0 >= 1_000 ? DisplayText.span(milliseconds: $0) : nil }
        switch health {
        case .starting:
            return "几秒后就能开始播放。"
        case .viewerPaused:
            if let prepared {
                return "后面的画面已经准备到 \(DisplayText.clock(milliseconds: prepared))，继续播放不用等。"
            }
            return "继续播放后接着转。"
        case .ahead:
            if let leadText {
                return "已经领先观众 \(leadText)，先歇一会儿，播放追上来就接着转。"
            }
            return "已经领先观众一两分钟，先歇一会儿，播放追上来就接着转。"
        case let .smooth(speed):
            if let leadText {
                return "转码比播放快 \(Health.times(speed))，已经领先观众 \(leadText)。"
            }
            return "转码比播放快 \(Health.times(speed))，正在提前准备后面的画面。"
        case .tight:
            return "转码速度刚够跟上播放，Mac 再忙一点就可能缓冲。"
        case .lagging:
            return "转码比播放慢，观众会遇到缓冲。关掉占用显卡的程序，或在设置里把「同时转码」调低。"
        }
    }

    static func resources(_ history: ResourceHistory) -> Resources? {
        guard let latest = history.samples.last else { return nil }
        let memories = history.samples.map { Double($0.transcoderMemory) }
        // 内存图按转码自己的峰值定刻度（至少 256 MB）：占整机的比例太小，按总内存画就是一条贴地的线
        let memoryScale = max((memories.max() ?? 0) * 1.25, 256 * 1_048_576)
        return Resources(
            cpu: .init(
                value: percent(latest.transcoderCPU),
                detail: "整机 \(percent(latest.systemCPU))",
                series: history.samples.map(\.transcoderCPU),
                background: history.samples.map(\.systemCPU),
                tooltip: "转码：正在转码的 ffmpeg 占整台 Mac CPU 的比例"
                    + "（\(history.cores) 个核合计 100%），蓝色是转码，灰色是整机。"
                    + "视频编码在 Apple 芯片的媒体引擎上完成，不计入 CPU。\n"
                    + "本 App 自身（界面 + 转码内核）：\(percent(latest.appCPU))，不算在转码里。"
            ),
            memory: .init(
                value: DisplayText.bytes(latest.transcoderMemory),
                detail: "整机 \(DisplayText.gigabytes(latest.systemMemory)) / "
                    + "\(DisplayText.gigabytes(history.physicalMemory, decimals: 0)) GB",
                series: memories.map { $0 / memoryScale },
                background: nil,
                tooltip: "转码：正在转码的 ffmpeg 用的内存，和「活动监视器」的「内存」一栏同口径。"
                    + "整机：已用 / 总内存。\n"
                    + "本 App 自身（界面 + 转码内核）：\(DisplayText.bytes(latest.appMemory))，不算在转码里。"
            )
        )
    }

    /// 「18%」；有占用但不到 1% 时写「<1%」，免得看着像完全没在跑。
    private static func percent(_ fraction: Double) -> String {
        let value = fraction * 100
        if value > 0, value < 1 { return "<1%" }
        return "\(Int(value.rounded()))%"
    }

    private static func summary(_ records: [JobRecord]) -> Summary? {
        guard !records.isEmpty else { return nil }
        return Summary(
            count: records.count,
            media: DisplayText.duration(milliseconds: records.reduce(Int64(0)) { $0 + $1.mediaMS }),
            failures: records.filter { $0.outcome == .failed }.count
        )
    }

    /// 最近几条：每条说清楚「这次怎么结束的」。远程转码是跟着播放走的，观众关掉播放器
    /// 就结束了——那是正常收尾。有观众位置时说「看到 25:10」（用户最关心的），没有时
    /// 说「转出 26 分钟的片子」：明说是片长，不会被当成「转了多久」。
    private static func history(_ records: [JobRecord], now: Date) -> [HistoryRow] {
        records.prefix(recentLimit).map { record in
            let lead: String
            switch record.outcome {
            case .failed:
                lead = "失败"
            case .finished:
                lead = "整部转完"
            case .stopped:
                if let watched = record.watchedMS {
                    lead = "看到 \(DisplayText.clock(milliseconds: watched))"
                } else if record.mediaMS < 60_000 {
                    lead = "转出不到 1 分钟的片子"
                } else {
                    lead = "转出 \(DisplayText.duration(milliseconds: record.mediaMS))的片子"
                }
            }
            let name = record.name.flatMap { $0.isEmpty ? nil : $0 }
            return HistoryRow(
                title: name.map(DisplayText.withoutExtension) ?? "任务 \(record.id.prefix(8))",
                outcome: record.outcome,
                detail: "\(lead) · \(DisplayText.relative(record.endedAt, now: now))",
                tooltip: [name, record.error].compactMap { $0 }.joined(separator: "\n")
            )
        }
    }

    /// 「ffmpeg 8.1.2 · 硬件编码 H.264 / HEVC」。版本号去掉「-Jellyfin」后缀：底栏很窄，
    /// 带上它硬件编码那半句就被截掉了；完整版本号在设置的「关于」页。
    private static func footnote(_ status: WorkerStatus) -> String? {
        var version = DisplayText.ffmpegVersion(status.ffmpegVersion)
        guard version != "-", !version.isEmpty else { return nil }
        if version.hasSuffix("-Jellyfin") {
            version = String(version.dropLast("-Jellyfin".count))
        }
        let hardware = DisplayText.hardwareCodecs(status.encoders)
        return (["ffmpeg \(version)"] + (hardware.isEmpty ? [] : ["硬件编码 " + hardware.joined(separator: " / ")]))
            .joined(separator: " · ")
    }
}

extension DisplayText {
    /// 去掉常见视频扩展名。片名本身可能带点（「Mr.Robot」），只认末尾那几种。
    static func withoutExtension(_ name: String) -> String {
        let lower = name.lowercased()
        for ext in [".mkv", ".mp4", ".m4v", ".mov", ".ts", ".m2ts", ".avi", ".iso", ".webm"]
        where lower.hasSuffix(ext) {
            return String(name.dropLast(ext.count))
        }
        return name
    }

    /// ffmpeg 编码器名 → 给人看的说法。
    static func encoder(_ raw: String?) -> String? {
        guard let raw, !raw.isEmpty else { return nil }
        if raw == "copy" { return "原样转发，不重新编码" }
        if let codec = videoToolboxCodec(raw) { return "用硬件转成 \(codec)" }
        return "转成 \(raw)"
    }

    /// 编码器列表里的 VideoToolbox 硬件编码器，按固定顺序给出人读的名字。
    static func hardwareCodecs(_ encoders: [String]) -> [String] {
        ["h264_videotoolbox", "hevc_videotoolbox", "prores_videotoolbox"]
            .filter(encoders.contains)
            .compactMap(videoToolboxCodec)
    }

    private static func videoToolboxCodec(_ encoder: String) -> String? {
        switch encoder {
        case "h264_videotoolbox": return "H.264"
        case "hevc_videotoolbox": return "HEVC"
        case "prores_videotoolbox": return "ProRes"
        default: return nil
        }
    }

    /// 片长总量：满一小时用小时（一位小数，整数时不带「.0」），否则用分钟。
    static func duration(milliseconds: Int64) -> String {
        let minutes = Double(max(0, milliseconds)) / 60_000
        if minutes >= 60 {
            let hours = (minutes / 6).rounded() / 10
            return hours == hours.rounded() ? "\(Int(hours)) 小时" : String(format: "%.1f 小时", hours)
        }
        return "\(Int(minutes.rounded())) 分钟"
    }

    /// 「23:10」
    static func timeOfDay(_ date: Date) -> String {
        let parts = Calendar.current.dateComponents([.hour, .minute], from: date)
        return String(format: "%d:%02d", parts.hour ?? 0, parts.minute ?? 0)
    }

    /// 「420 MB」「1.2 GB」
    static func bytes(_ value: UInt64) -> String {
        let megabytes = Double(value) / 1_048_576
        return megabytes < 1_024
            ? "\(Int(megabytes.rounded())) MB"
            : String(format: "%.1f GB", megabytes / 1_024)
    }

    /// 以 GB 计的数字（不带单位）：「12.2」
    static func gigabytes(_ value: UInt64, decimals: Int = 1) -> String {
        String(format: "%.\(decimals)f", Double(value) / 1_073_741_824)
    }

    /// 一段时长的口语说法：「45 秒」「1 分 50 秒」「3 分钟」。
    static func span(milliseconds: Int64) -> String {
        let seconds = Int(max(0, milliseconds) / 1_000)
        if seconds < 60 { return "\(seconds) 秒" }
        let minutes = seconds / 60, rest = seconds % 60
        return rest == 0 ? "\(minutes) 分钟" : "\(minutes) 分 \(rest) 秒"
    }

    /// 相对时间：刚刚 / N 分钟前 / N 小时前 / 昨天 / M月d日。
    static func relative(_ date: Date, now: Date, calendar: Calendar = .current) -> String {
        let seconds = now.timeIntervalSince(date)
        if seconds < 60 { return "刚刚" }
        if seconds < 3_600 { return "\(Int(seconds / 60)) 分钟前" }
        if calendar.isDate(date, inSameDayAs: now) { return "\(Int(seconds / 3_600)) 小时前" }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now),
           calendar.isDate(date, inSameDayAs: yesterday) {
            return "昨天"
        }
        let parts = calendar.dateComponents([.month, .day], from: date)
        return "\(parts.month ?? 0)月\(parts.day ?? 0)日"
    }
}
