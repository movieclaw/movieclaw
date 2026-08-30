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

enum WorkerConnectionState: String, Sendable {
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
}

struct JobProgress: Sendable {
    let outTimeMS: Int64?
    let speed: String?
    let phase: String?
}

struct WorkerStatus: Sendable {
    let state: WorkerConnectionState
    let message: String
    let workerID: String
    let activeJobs: Int
    let maxJobs: Int
    let currentJobID: String?
    /// 源文件名，服务端下发；旧版服务端为 nil，此时回退显示 job id。
    let currentJobName: String?
    let currentProgress: JobProgress?
    let ffmpegVersion: String
    let encoders: [String]
    let lastError: String?
    let updatedAt: Date
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
