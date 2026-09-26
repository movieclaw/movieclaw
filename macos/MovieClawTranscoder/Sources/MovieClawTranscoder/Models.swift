import Foundation

/// Worker 运行所需的最小配置。
///
/// 菜单栏模式下 Token 只在内存中的这个结构体里出现，持久化由
/// ConfigurationStore + Keychain 负责；Headless 模式只使用显式命令行参数，
/// 不读取环境变量。
struct WorkerConfiguration: Sendable {
    let nasURL: URL
    let workerToken: String
    let workerID: String
    let ffmpegPath: String
    let maxJobs: Int

    /// 是否使用仅适合可信内网的明文 HTTP 传输。
    ///
    /// 该模式是显式配置才会启用的诊断/内网模式，不做 HTTPS 到 HTTP 的自动降级，
    /// 避免公网部署因为代理故障而悄悄泄露源视频、Token 和控制消息。
    var usesInsecureHTTP: Bool {
        nasURL.scheme?.lowercased() == "http"
    }

    init(
        nasURL: URL,
        workerToken: String,
        workerID: String,
        ffmpegPath: String,
        maxJobs: Int
    ) {
        self.nasURL = nasURL
        self.workerToken = workerToken
        self.workerID = workerID
        self.ffmpegPath = ffmpegPath
        self.maxJobs = max(1, min(4, maxJobs))
    }

    static func make(
        nasText: String,
        token: String,
        workerID: String,
        ffmpegPath: String,
        maxJobs: Int
    ) throws -> WorkerConfiguration {
        let nasURL = try normalizedNASURL(nasText)
        let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedToken.isEmpty else {
            throw ConfigurationError.message("这台 Mac 还没有配对，请在设置里完成配对")
        }
        return WorkerConfiguration(
            nasURL: nasURL,
            workerToken: trimmedToken,
            workerID: try validatedWorkerID(workerID),
            ffmpegPath: try validatedFFmpegPath(ffmpegPath),
            maxJobs: maxJobs
        )
    }

    static func load(arguments: [String]) throws -> WorkerConfiguration {
        let values = try ArgumentParser(arguments: Array(arguments.dropFirst())).parse()
        guard let nasText = values["nas-url"] else {
            throw ConfigurationError.message("缺少 --nas-url")
        }
        let token = values["token"] ?? ""
        let defaultID = Host.current().localizedName ?? "mac-worker"
        let workerID = values["worker-id"]
            ?? defaultID.replacingOccurrences(of: " ", with: "-")
        let ffmpegPath = values["ffmpeg"]
            ?? defaultFFmpegPath()
        let maxJobs = Int(values["max-jobs"] ?? "1") ?? 1
        return try make(
            nasText: nasText,
            token: token,
            workerID: workerID,
            ffmpegPath: ffmpegPath,
            maxJobs: maxJobs
        )
    }

    static func defaultWorkerID() -> String {
        let name = Host.current().localizedName ?? "mac-worker"
        return sanitizedWorkerID(name)
    }

    /// 把机器名收敛成 validatedWorkerID 能接受的形状。
    ///
    /// 只替换空格是不够的：中文环境下 Mac 默认就叫「张三的Mac mini」，
    /// 「的」过不了 ASCII 白名单，首次打开点「连接并配对」就会被拦下，
    /// 而用户完全没改过这个字段，根本想不到问题出在机器名上。
    static func sanitizedWorkerID(_ name: String) -> String {
        let allowed = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-")
        var result = String(name.map { allowed.contains($0) ? $0 : "-" })
        while result.contains("--") {
            result = result.replacingOccurrences(of: "--", with: "-")
        }
        result = result.trimmingCharacters(in: CharacterSet(charactersIn: "-_.:"))
        // 首字符必须是字母或数字；整串被清空（例如纯中文名）时退回通用名
        guard let first = result.first, first.isLetter || first.isNumber else {
            return "mac-worker"
        }
        return String(result.prefix(64))
    }

    static func defaultFFmpegPath() -> String {
        "/opt/homebrew/bin/jellyfin-ffmpeg"
    }

    static func printUsage() {
        print("""
        MovieClawTranscoder

        菜单栏 App：直接打开 MovieClawTranscoder.app，在设置里填 movieclaw 地址，
        按提示到网页「设置 → 设备」批准配对即可，不需要手工填任何令牌。

        无界面模式（无人值守部署；令牌请在网页「设置 → 设备」创建）：
          movieclaw-transcoder --headless --nas-url https://nas.example.com --token <token>
                               [--ffmpeg /opt/homebrew/bin/jellyfin-ffmpeg]
                               [--worker-id macmini-m1] [--max-jobs 1]

        Headless 模式只接受上述命令行参数，不读取环境变量。
        """)
    }

    static func validatedWorkerID(_ workerID: String) throws -> String {
        let trimmed = workerID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.range(of: "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$", options: .regularExpression) != nil else {
            throw ConfigurationError.message("Worker 名称只能包含字母、数字、下划线、点、冒号和短横线")
        }
        return trimmed
    }

    static func validatedFFmpegPath(_ ffmpegPath: String) throws -> String {
        let path = ffmpegPath.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !path.isEmpty else {
            throw ConfigurationError.message("Jellyfin-ffmpeg 路径不能为空")
        }
        return path
    }

    static func normalizedNASURL(_ text: String) throws -> URL {
        var value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        while value.hasSuffix("/") {
            value.removeLast()
        }
        guard let url = URL(string: value),
              let scheme = url.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              url.host != nil
        else {
            throw ConfigurationError.message("NAS 地址必须是带主机名的 HTTP 或 HTTPS 地址")
        }
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil
        else {
            throw ConfigurationError.message("NAS 地址不能包含用户名、密码、查询参数或片段")
        }
        var normalized = components
        normalized.scheme = scheme
        return normalized.url ?? url
    }

    static func isInsecureHTTPAddress(_ text: String) -> Bool {
        var value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        while value.hasSuffix("/") {
            value.removeLast()
        }
        return URL(string: value)?.scheme?.lowercased() == "http"
    }
}

enum WorkerConnectionState: String, Sendable, Codable {
    case unconfigured
    case starting
    case connecting
    case ready
    case busy
    case paused
    case draining
    case reconnecting
    case stopped
    case error

    var displayName: String {
        switch self {
        case .unconfigured: return "未配置"
        case .starting: return "启动中"
        case .connecting: return "连接中"
        case .ready: return "已连接"
        case .busy: return "转码中"
        case .paused: return "已暂停"
        case .draining: return "排空中"
        case .reconnecting: return "等待重连"
        case .stopped: return "已停止"
        case .error: return "错误"
        }
    }
}

enum FFmpegSource: String, Sendable, Equatable {
    case custom
    case managed
}

/// 菜单栏的 Jellyfin-ffmpeg 入口状态。
enum FFmpegMenuState: Sendable, Equatable {
    case download
    case update(version: String?)
    case processing
    case retry(hasManagedVersion: Bool)

    var title: String {
        switch self {
        case .download:
            return "下载 Jellyfin-ffmpeg"
        case .update:
            return "更新 Jellyfin-ffmpeg"
        case .processing:
            return "正在处理 Jellyfin-ffmpeg…"
        case let .retry(hasManagedVersion):
            return hasManagedVersion ? "重试更新 Jellyfin-ffmpeg" : "重试下载 Jellyfin-ffmpeg"
        }
    }

    var isEnabled: Bool {
        if case .processing = self { return false }
        return true
    }

    /// 菜单项图标（SF Symbols）。
    var symbolName: String {
        switch self {
        case .download: return "arrow.down.circle"
        case .update: return "arrow.triangle.2.circlepath"
        case .processing: return "hourglass"
        case .retry: return "arrow.clockwise.circle"
        }
    }
}

struct JobProgress: Sendable, Equatable, Codable {
    /// ffmpeg 的输出进度（**毫秒**），**从这一轮起转的位置算起**，不是片内时间：
    /// 拖进度条后 NAS 用 `-ss` 从新位置重启 ffmpeg，它就从 0 重新数。要换成片内
    /// 位置得加上起点，见 ``RunningJob/startOffsetMS``。
    let outTimeMS: Int64?
    let speed: String?
    let phase: String?

    /// 从 ffmpeg `-progress` 的一组键值里取输出位置，换算成毫秒。
    ///
    /// **`out_time_ms` 名不副实，单位其实是微秒**（ffmpeg 的历史遗留，为兼容一直没改，
    /// 所以又加了一个 `out_time_us`）。早先直接当毫秒用，片内位置与「转出多长的片子」
    /// 全都大了 1000 倍——面板上出现过「转了 20.4 小时」，实际只转了 73 秒。
    /// 依次认 `out_time_us`、`out_time_ms`（都是微秒），都没有再解析 `out_time`（时:分:秒）。
    static func outTimeMilliseconds(_ values: [String: String]) -> Int64? {
        for key in ["out_time_us", "out_time_ms"] {
            if let micro = values[key].flatMap({ Int64($0) }), micro >= 0 {
                return micro / 1_000
            }
        }
        guard let text = values["out_time"] else { return nil }
        let parts = text.split(separator: ":")
        guard parts.count == 3, let h = Double(parts[0]), let m = Double(parts[1]), let s = Double(parts[2]),
              h >= 0, m >= 0, s >= 0
        else { return nil }
        return Int64(((h * 60 + m) * 60 + s) * 1_000)
    }
}

/// NAS 推来的观众播放位置（`job.playback`，毫秒，片内时间）。拿不到的字段为 nil：
/// 非 VOD 会话没有总长与准备位置，播放器没上报过进度时没有观众位置。
struct JobPlayback: Sendable, Equatable, Codable {
    let positionMS: Int64?
    let viewerPaused: Bool
    let durationMS: Int64?
    let preparedMS: Int64?
}

/// 一个正在跑的任务，给状态面板和菜单栏图标用。
struct RunningJob: Sendable, Equatable, Codable {
    let id: String
    /// 源文件名，服务端下发；旧版服务端为 nil，此时回退显示 job id。
    let name: String?
    let progress: JobProgress?
    /// 这一轮 ffmpeg 起转的时间（seek 重启会刷新）。
    let startedAt: Date
    /// ffmpeg 参数里的视频编码器（`-c:v` 的值，如 h264_videotoolbox）。
    let videoEncoder: String?
    /// 这一轮从片子的哪个位置起转（毫秒，取自 ffmpeg 参数里输入前的 `-ss`）。
    /// 片内位置 = 起点 + ``JobProgress/outTimeMS``。
    var startOffsetMS: Int64 = 0
    /// NAS 让它歇着（转码头领先播放足够远、或磁盘吃紧），之后会自动续上。
    let paused: Bool
    /// 观众看到哪儿了。旧版服务端不推，为 nil。
    var playback: JobPlayback? = nil
}

/// 需要让用户知道、面板要专门说明的故障（普通的断线重连不算）。
///
/// 每一种都对应一个明确的下一步：要么 App 自己在恢复（告诉用户别慌），要么只有
/// 用户能处理（告诉他点哪里）。见 docs/design/remote-transcode.md「Worker 容错」。
enum WorkerProblem: Sendable, Equatable, Codable {
    /// NAS 拒绝了这台 Mac 的凭证（被吊销或失效）：多半要重新配对，放慢到每 5 分钟重试一次。
    case authRejected
    /// 服务端没打开远程转码：放慢重试（每分钟一次），等管理员打开。
    case remoteDisabled
    /// 连续几个任务刚开始就失败：暂停接单自检，`until` 之后自动恢复。
    case cooldown(until: Date, failures: Int)
    /// 转码内核刚异常退出，正在自动恢复（第 `attempt` 次）。
    case coreRecovering(attempt: Int)
    /// 转码内核短时间内反复崩溃，已停止自动重启，等用户点「重试」。
    case coreCrashLoop
    /// ffmpeg 用不了（找不到、自检失败）。
    case ffmpegUnusable
}

struct WorkerStatus: Sendable, Codable {
    let state: WorkerConnectionState
    let message: String
    let workerID: String
    let maxJobs: Int
    /// 正在跑的任务，按 job id 排序，顺序稳定、面板上的卡片不会跳来跳去。
    let jobs: [RunningJob]
    let ffmpegVersion: String
    let encoders: [String]
    let lastError: String?
    let updatedAt: Date
    var problem: WorkerProblem? = nil

    var activeJobs: Int { jobs.count }

    /// Worker 没在跑时（启动前、停止后、启动失败）由 AppMain 自己拼的状态。
    static func offline(
        _ state: WorkerConnectionState,
        message: String,
        workerID: String,
        maxJobs: Int,
        ffmpegVersion: String = "-",
        error: String? = nil,
        problem: WorkerProblem? = nil
    ) -> WorkerStatus {
        WorkerStatus(
            state: state,
            message: message,
            workerID: workerID,
            maxJobs: maxJobs,
            jobs: [],
            ffmpegVersion: ffmpegVersion,
            encoders: [],
            lastError: error,
            updatedAt: Date(),
            problem: problem
        )
    }
}

/// 配置与运行期的可读错误。
///
/// **必须实现 LocalizedError**：界面和日志一律走 `error.localizedDescription`，
/// 而它对普通 Error 返回的是「The operation couldn't be completed.
/// (MovieClawTranscoder.ConfigurationError error 0.)」——精心写好的中文提示
/// 一个字都到不了用户眼前。只实现 CustomStringConvertible 不够，那条路径
/// 没人走。
enum ConfigurationError: Error, LocalizedError, CustomStringConvertible {
    case message(String)

    var description: String {
        switch self {
        case let .message(text): return text
        }
    }

    var errorDescription: String? { description }
}

private struct ArgumentParser {
    let arguments: [String]

    func parse() throws -> [String: String] {
        var result: [String: String] = [:]
        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            guard argument.hasPrefix("--") else {
                throw ConfigurationError.message("不认识的参数：\(argument)")
            }
            let key = String(argument.dropFirst(2))
            if key == "help" {
                WorkerConfiguration.printUsage()
                exit(0)
            }
            if key == "headless" {
                result[key] = "true"
                index += 1
                continue
            }
            guard index + 1 < arguments.count, !arguments[index + 1].hasPrefix("--") else {
                throw ConfigurationError.message("参数 --\(key) 缺少值")
            }
            result[key] = arguments[index + 1]
            index += 2
        }
        return result
    }
}
