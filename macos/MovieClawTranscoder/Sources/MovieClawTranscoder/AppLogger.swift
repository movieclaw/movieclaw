import Foundation
import OSLog

enum LogSanitizer {
    private static let tokenQueryPattern = try! NSRegularExpression(pattern: #"(?i)(token=)[^&\s]+"#)

    static func redact(_ text: String, secret: String? = nil) -> String {
        var value = text
        let range = NSRange(value.startIndex..<value.endIndex, in: value)
        value = tokenQueryPattern.stringByReplacingMatches(
            in: value,
            range: range,
            withTemplate: "$1<redacted>"
        )
        if let secret, !secret.isEmpty {
            value = value.replacingOccurrences(of: secret, with: "<redacted>")
        }
        return value
    }
}

/// 同时写统一日志和一个小型轮转文件，便于用户在没有终端的情况下反馈问题。
final class AppLogger: @unchecked Sendable {
    static let shared = AppLogger()

    let logURL: URL
    private let osLogger = Logger(subsystem: "com.movieclaw.transcoder", category: "worker")
    private let lock = NSLock()
    private let formatter: ISO8601DateFormatter

    private init() {
        logURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/MovieClawTranscoder.log")
        formatter = ISO8601DateFormatter()
    }

    func info(_ message: String, secret: String? = nil) {
        write("INFO", message, secret: secret)
        osLogger.info("\(LogSanitizer.redact(message, secret: secret), privacy: .public)")
    }

    func warning(_ message: String, secret: String? = nil) {
        write("WARN", message, secret: secret)
        osLogger.warning("\(LogSanitizer.redact(message, secret: secret), privacy: .public)")
    }

    func error(_ message: String, secret: String? = nil) {
        write("ERROR", message, secret: secret)
        osLogger.error("\(LogSanitizer.redact(message, secret: secret), privacy: .public)")
    }

    private func write(_ level: String, _ message: String, secret: String?) {
        let safeMessage = LogSanitizer.redact(message, secret: secret)
        lock.lock()
        defer { lock.unlock() }
        let line = "[\(formatter.string(from: Date()))] [\(level)] \(safeMessage)\n"
        do {
            let directory = logURL.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            if let attributes = try? FileManager.default.attributesOfItem(atPath: logURL.path),
               let size = attributes[.size] as? NSNumber,
               size.intValue > 5_000_000 {
                let rotated = logURL.appendingPathExtension("1")
                try? FileManager.default.removeItem(at: rotated)
                try? FileManager.default.moveItem(at: logURL, to: rotated)
            }
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
                try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: logURL.path)
            }
            let handle = try FileHandle(forWritingTo: logURL)
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: Data(line.utf8))
        } catch {
            // 日志不能反过来阻塞 Worker；统一日志仍然已经记录了主消息。
        }
    }
}
