import Darwin
import Foundation

// 容错的几条判断规则，全部写成纯逻辑（输入时间、输出决定），便于单测钉住边界。
// 整体设计见 docs/design/remote-transcode.md「Worker 容错」：
//
// 1. 单个任务：卡死看门狗（``JobWatchdog``）、连续失败熔断（``FailureBreaker``）、
//    孤儿 ffmpeg 清理（``OrphanSweeper``）；
// 2. 内核进程：崩溃自动重启、崩溃循环保护（``CrashPolicy``）、卡死检测（``CoreSupervisor``）；
// 3. App 本身：系统登录项负责开机启动与崩溃后拉起（``LoginItem``），防多开。

/// 转码内核崩溃后的重启节奏：1、2、4、8…秒，最长 60 秒；10 分钟内崩溃满 5 次就不再
/// 自动重启——那多半是环境本身坏了（ffmpeg 损坏、系统库问题），无限重启只会刷屏、烧 CPU，
/// 这时把原因摆出来、等用户点「重试」更有用。
struct CrashPolicy {
    enum Decision: Equatable {
        case restart(after: TimeInterval, attempt: Int)
        case giveUp
    }

    static let window: TimeInterval = 600
    static let limit = 5
    static let maxDelay: TimeInterval = 60

    private(set) var crashes: [Date] = []

    mutating func recordCrash(at now: Date) -> Decision {
        crashes = crashes.filter { now.timeIntervalSince($0) < Self.window } + [now]
        guard crashes.count < Self.limit else { return .giveUp }
        let delay = min(Self.maxDelay, pow(2, Double(crashes.count - 1)))
        return .restart(after: delay, attempt: crashes.count)
    }

    mutating func reset() {
        crashes.removeAll()
    }
}

/// 连续失败熔断：连续几个任务都是「刚开始就失败」，说明这台 Mac 出了问题（ffmpeg 起不来、
/// 硬件编码器打不开），继续接单只会让每次播放都先失败一遍再降档。这时暂停接单、做一次自检。
///
/// - 「刚开始就失败」= 失败且只跑了不到 20 秒。跑了很久才失败（网络断了、看门狗杀掉）
///   不算：那不是这台 Mac 的毛病。
/// - 转完、或者正常跑了一阵后被 NAS 叫停，都说明 ffmpeg 是好的，计数清零。
/// - 刚起转就被叫停（拖进度条的重启）不说明任何问题，不影响计数。
struct FailureBreaker {
    static let threshold = 3
    static let window: TimeInterval = 300
    static let quickFailure: TimeInterval = 20
    static let cooldown: TimeInterval = 600

    private(set) var quickFailures: [Date] = []

    /// 记一笔。返回 true 表示该熔断了。
    mutating func record(_ outcome: JobRecord.Outcome, ranFor: TimeInterval, at now: Date) -> Bool {
        let quick = ranFor < Self.quickFailure
        switch outcome {
        case .failed where quick:
            quickFailures = quickFailures.filter { now.timeIntervalSince($0) < Self.window } + [now]
            return quickFailures.count >= Self.threshold
        case .stopped where quick:
            return false
        default:
            quickFailures.removeAll()
            return false
        }
    }

    mutating func reset() {
        quickFailures.removeAll()
    }
}

/// 任务卡死看门狗：任务在跑（没被 NAS 暂停）却长时间没有任何进度，就是 ffmpeg 卡住了
/// ——硬件编码器挂住、读源卡在某个系统调用里。ffmpeg 自己的网络超时（30 秒）管不到这些。
/// 杀掉它、把原因报给 NAS，NAS 会按已有逻辑重试或降档；不杀的话这个任务永远占着槽位。
enum JobWatchdog {
    /// 起转后第一条进度最多等这么久（要连上源、探测格式、初始化硬件编码器）。
    static let firstProgressLimit: TimeInterval = 90
    /// 之后两条进度之间最多隔这么久。
    static let progressLimit: TimeInterval = 60

    /// 该不该杀；该杀就返回写进失败原因的那句话。
    static func verdict(startedAt: Date, lastProgressAt: Date?, paused: Bool, now: Date) -> String? {
        guard !paused else { return nil }
        if let lastProgressAt {
            let silent = now.timeIntervalSince(lastProgressAt)
            guard silent > progressLimit else { return nil }
            return "ffmpeg 已经 \(Int(silent)) 秒没有任何进度（卡住了），Worker 已强制结束它"
        }
        let waited = now.timeIntervalSince(startedAt)
        guard waited > firstProgressLimit else { return nil }
        return "ffmpeg 起转 \(Int(waited)) 秒仍没有任何进度（卡住了），Worker 已强制结束它"
    }
}

/// NAS 拒绝连接的理由分类（WebSocket 1008 关闭帧里的那句话）。
///
/// 靠文字匹配是不得已：服务端只在关闭理由里说原因，没有单独的错误码。匹配的是服务端
/// `transcode_worker.py` 里写死的那几句，改那边的文案时要一起看这里。
enum NASRejection: Equatable {
    /// 凭证无效或已被吊销：多半要重新配对，放慢到每 5 分钟重试一次。
    case authRejected
    /// 服务端没打开远程转码：等管理员打开，慢慢重试。
    case remoteDisabled
    /// 其他（协议版本不一致、握手被 HTTP 403 / 404 拒绝……）：每分钟重试，把原因摆出来。
    case other

    init(reason: String) {
        if reason.contains("凭证") || reason.contains("吊销") {
            self = .authRejected
        } else if reason.contains("尚未启用远程转码") {
            self = .remoteDisabled
        } else {
            self = .other
        }
    }
}

/// 孤儿 ffmpeg 清理：内核崩溃时，它起的 ffmpeg 会被系统过继给 launchd 继续跑——
/// 还在读源、传分片、占 CPU，被暂停（SIGSTOP）的更是永远挂在那里。新内核启动时
/// 把它们找出来结束掉。
///
/// 认定条件三条同时满足，宁可漏杀也不误杀：父进程已是 launchd（pid 1）、可执行文件
/// 就是配置里的 ffmpeg、参数里带着 NAS 转码接口的取源地址（`/transcode-worker/`）和
/// `-progress pipe:1`——用户自己在终端里跑的 ffmpeg 不会同时满足。
enum OrphanSweeper {
    struct ProcessRecord: Equatable {
        var pid: pid_t
        var parentPID: pid_t
        var executablePath: String
        var arguments: [String]
    }

    static func isOrphanedWorkerFFmpeg(_ process: ProcessRecord, ffmpegPath: String) -> Bool {
        guard process.parentPID == 1 else { return false }
        guard resolved(process.executablePath) == resolved(ffmpegPath) else { return false }
        let joined = process.arguments.joined(separator: " ")
        return joined.contains("/transcode-worker/") && joined.contains("-progress pipe:1")
    }

    /// 找出并结束孤儿，返回结束了的进程号。
    @discardableResult
    static func sweep(ffmpegPath: String) -> [pid_t] {
        let victims = allProcesses().filter { isOrphanedWorkerFFmpeg($0, ffmpegPath: ffmpegPath) }
        for victim in victims {
            kill(victim.pid, SIGKILL)
        }
        return victims.map(\.pid)
    }

    private static func resolved(_ path: String) -> String {
        URL(fileURLWithPath: path).resolvingSymlinksInPath().path
    }

    private static func allProcesses() -> [ProcessRecord] {
        let capacity = Int(proc_listallpids(nil, 0)) + 64
        guard capacity > 64 else { return [] }
        var pids = [pid_t](repeating: 0, count: capacity)
        let count = pids.withUnsafeMutableBytes {
            proc_listallpids($0.baseAddress, Int32($0.count))
        }
        return pids.prefix(Int(max(0, count))).compactMap(info(of:))
    }

    private static func info(of pid: pid_t) -> ProcessRecord? {
        guard pid > 1 else { return nil }
        var bsd = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &bsd, size) == size else { return nil }
        var path = [CChar](repeating: 0, count: Int(MAXPATHLEN) * 4)
        guard proc_pidpath(pid, &path, UInt32(path.count)) > 0 else { return nil }
        return ProcessRecord(
            pid: pid,
            parentPID: pid_t(bsd.pbi_ppid),
            executablePath: String(cString: path),
            arguments: arguments(of: pid)
        )
    }

    /// 读另一个（同用户）进程的命令行参数：sysctl KERN_PROCARGS2 的格式是
    /// argc（Int32）、可执行文件路径、若干个 \0 填充，然后 argc 个以 \0 结尾的参数。
    private static func arguments(of pid: pid_t) -> [String] {
        var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
        var size = 0
        guard sysctl(&mib, 3, nil, &size, nil, 0) == 0, size > MemoryLayout<Int32>.size else { return [] }
        var buffer = [UInt8](repeating: 0, count: size)
        guard sysctl(&mib, 3, &buffer, &size, nil, 0) == 0 else { return [] }
        let argc = buffer.withUnsafeBytes { $0.load(as: Int32.self) }
        var index = MemoryLayout<Int32>.size
        while index < size, buffer[index] != 0 { index += 1 }   // 可执行文件路径
        while index < size, buffer[index] == 0 { index += 1 }   // 填充
        var result: [String] = []
        while index < size, result.count < argc {
            let start = index
            while index < size, buffer[index] != 0 { index += 1 }
            result.append(String(decoding: buffer[start..<index], as: UTF8.self))
            index += 1
        }
        return result
    }
}
