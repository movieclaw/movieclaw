import Foundation

struct WorkerCapabilities: Sendable {
    let ffmpegVersion: String
    let encoders: [String]
    let backends: [String]
}

enum CapabilityProbe {
    static func run(ffmpegPath: String) throws -> WorkerCapabilities {
        guard FileManager.default.isExecutableFile(atPath: ffmpegPath) else {
            throw ConfigurationError.message("找不到可执行的 ffmpeg：\(ffmpegPath)")
        }
        let versionOutput = try execute(ffmpegPath, arguments: ["-version"])
        let encoderOutput = try execute(ffmpegPath, arguments: ["-hide_banner", "-encoders"])
        let version = versionOutput
            .split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: true)
            .first
            .map(String.init) ?? "unknown"
        let encoders = Self.parseEncoders(encoderOutput)
        let backends = encoders.contains("h264_videotoolbox") ? ["videotoolbox"] : []
        guard !backends.isEmpty else {
            throw ConfigurationError.message(
                "当前 ffmpeg 没有 h264_videotoolbox，不能把它登记为硬件转码 Worker"
            )
        }
        return WorkerCapabilities(
            ffmpegVersion: version,
            encoders: encoders,
            backends: backends
        )
    }

    /// 从 `ffmpeg -encoders` 的输出里取编码器名。
    ///
    /// 输出开头是 8 行标志位图例（` V..... = Video`、` .F.... = Frame-level
    /// multithreading` …），它们的第一列同样是 6 个字符，只按列宽过滤会把
    /// 图例的 `=` 当成 8 个编码器收进来（issue #286 的能力快照里
    /// `encoders==,=,=,…` 就是这么来的）。图例行第二列固定是 `=`，据此排除；
    /// `------` 分隔行只有一列，本来就过不了。
    static func parseEncoders(_ output: String) -> [String] {
        output
            .split(separator: "\n")
            .compactMap { line -> String? in
                let fields = line.split(whereSeparator: { $0 == " " || $0 == "\t" })
                guard fields.count >= 2, fields[0].count == 6, fields[1] != "=" else { return nil }
                return String(fields[1])
            }
    }

    private static func execute(_ path: String, arguments: [String]) throws -> String {
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = output
        try process.run()
        process.waitUntilExit()
        let text = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)
            ?? ""
        guard process.terminationStatus == 0 else {
            throw ConfigurationError.message("ffmpeg 能力探测失败：\(text.suffix(600))")
        }
        return text
    }
}
