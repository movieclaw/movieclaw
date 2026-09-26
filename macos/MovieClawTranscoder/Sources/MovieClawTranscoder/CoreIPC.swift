import Foundation

/// 菜单栏 App（界面进程）与转码内核进程之间的消息，每行一个 JSON。
///
/// ## 为什么拆成两个进程
///
/// 连 NAS、管 ffmpeg 的「内核」出了致命错误（崩溃、卡死）时，只重启内核，菜单栏 App
/// 一直在：面板照常能打开、能看到「正在自动恢复」，而不是图标消失、得等系统重新拉起。
/// 内核就是同一个可执行文件以 `--core` 模式启动（见 ``CoreRunner``），不带任何界面，
/// 实测空闲只占 5 MB 左右。看管它的是界面进程里的 ``CoreSupervisor``。
///
/// ## 通道
///
/// - 界面 → 内核：内核的标准输入。第一行必须是 ``CoreCommand/configure(_:)``（令牌走管道，
///   不进命令行参数和环境变量，`ps` 看不到）。界面进程一退出管道就断，内核读到 EOF
///   自己收尾退出，不会留下没人管的内核。
/// - 内核 → 界面：内核的标准输出。状态每变一次发一条，另外每 10 秒发一条 ``CoreEvent/alive``
///   报平安——这一条要先经过内核里的 WorkerClient，它卡死了平安就报不出来，界面据此判定卡死。
/// - 日志不走管道：两边都直接写同一个日志文件（O_APPEND，行级互不覆盖）。
///
/// 这套消息只在同一个 App 包的两个进程之间用，两边永远是同一版本，不需要兼容旧格式。
enum CoreCommand: Codable, Equatable {
    case configure(CoreConfiguration)
    /// 更新 ffmpeg 前暂停接单（手上的任务照常转完）。
    case setDraining(Bool)
    /// 睡眠唤醒、网络恢复：别等退避，立刻确认连接还活着。
    case reconnectNow
    /// 优雅退出：先跟 NAS 道别、停掉手上的 ffmpeg。
    case shutdown
}

/// 内核运行需要的配置。和 ``WorkerConfiguration`` 一一对应，只是能过管道。
struct CoreConfiguration: Codable, Equatable {
    var nasURL: String
    var token: String
    var workerID: String
    var ffmpegPath: String
    var maxJobs: Int

    init(_ configuration: WorkerConfiguration) {
        nasURL = configuration.nasURL.absoluteString
        token = configuration.workerToken
        workerID = configuration.workerID
        ffmpegPath = configuration.ffmpegPath
        maxJobs = configuration.maxJobs
    }

    func workerConfiguration() throws -> WorkerConfiguration {
        try WorkerConfiguration.make(
            nasText: nasURL, token: token, workerID: workerID, ffmpegPath: ffmpegPath, maxJobs: maxJobs
        )
    }
}

enum CoreEvent: Codable {
    case status(WorkerStatus)
    /// 一个任务结束了（转完 / 被叫停 / 失败），界面进程据此记进本地的任务记录。
    case jobEnded(JobRecord)
    /// 报平安（见上）。
    case alive
}

/// 内核的退出码。除了这几种「按设计退出」，其余的退出码和被信号杀掉都算崩溃，要自动重启。
enum CoreExitCode: Int32 {
    case normal = 0
    /// 配置无效（地址或名称不合法）：重启也没用。
    case invalidConfiguration = 64
    /// ffmpeg 找不到或自检失败：重启也没用，等用户换 / 重新下载 ffmpeg。
    case ffmpegUnusable = 65
}

/// 一行一个 JSON 的编解码。
enum CoreLine {
    static func encode<T: Encodable>(_ value: T) throws -> Data {
        var data = try JSONEncoder().encode(value)
        data.append(0x0A)
        return data
    }

    static func decode<T: Decodable>(_ type: T.Type, from line: Data) throws -> T {
        try JSONDecoder().decode(type, from: line)
    }

    /// 从缓冲区里切出所有完整的行（不含换行符），剩下的半行留在缓冲区里等下次。
    static func takeLines(from buffer: inout Data) -> [Data] {
        var lines: [Data] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<newline]
            if !line.isEmpty {
                lines.append(Data(line))
            }
            buffer.removeSubrange(buffer.startIndex...newline)
        }
        return lines
    }
}
