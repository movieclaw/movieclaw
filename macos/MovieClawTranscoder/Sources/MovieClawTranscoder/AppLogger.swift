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

    /// 安装未捕获异常的落盘钩子，让「App 突然消失」至少在自己的日志里留一行。
    ///
    /// 这次的教训（2026-08-30）：进程被 Objective-C 异常打死时（例如往已经
    /// ``invalidateAndCancel()`` 的 URLSession 上新建任务），Swift 的 catch 与
    /// defer 都不会执行，App 自己的日志里一个字都不会留下——用户看到的只是
    /// 「一 seek，菜单栏图标没了」，排查还得去翻
    /// ``~/Library/Logs/DiagnosticReports`` 里的 .ips 崩溃报告。这里在 abort
    /// 之前把异常名、原因和调用栈同步写进日志文件（``write`` 是同步落盘的，
    /// 不会来不及）。
    static func installCrashDiagnostics() {
        NSSetUncaughtExceptionHandler { exception in
            let stack = exception.callStackSymbols.prefix(20).joined(separator: " | ")
            AppLogger.shared.error(
                "未捕获异常导致进程退出：\(exception.name.rawValue) "
                + "reason=\(exception.reason ?? "未知") stack=\(stack)"
            )
        }
        installSignalBreadcrumb()
    }

    /// 致命信号的落盘面包屑，补 ``NSSetUncaughtExceptionHandler`` 覆盖不到的那一半。
    ///
    /// ObjC 异常之外，Swift 自己的 ``fatalError``、强解包 nil、数组越界都不走
    /// 异常，而是直接 SIGILL/SIGTRAP 陷入；野指针则是 SIGSEGV/SIGBUS。这些
    /// 情况下 App 的日志同样一个字都没有，用户只能描述成「图标突然没了」。
    ///
    /// 信号处理函数里只能调用 async-signal-safe 的函数，所以这里：
    /// * 日志 fd 和调用栈缓冲区都在安装时就备好，处理函数内不做任何内存分配；
    /// * 只用 ``write`` 和 ``backtrace_symbols_fd`` 输出，不碰 AppLogger 的锁；
    /// * 写完把信号恢复成默认处置再 ``raise``，保证系统的 .ips 崩溃报告照常生成
    ///   ——那份报告有完整符号，本面包屑只负责让人第一眼知道「它是崩掉的」。
    private static func installSignalBreadcrumb() {
        crashLogFD = open(shared.logURL.path, O_WRONLY | O_APPEND | O_CREAT, 0o600)
        guard crashLogFD >= 0 else { return }
        for signalNumber in [SIGABRT, SIGILL, SIGTRAP, SIGSEGV, SIGBUS, SIGFPE] {
            signal(signalNumber) { received in
                let fd = AppLogger.crashLogFD
                guard fd >= 0 else {
                    signal(received, SIG_DFL)
                    raise(received)
                    return
                }
                let line: StaticString
                switch received {
                case SIGILL, SIGTRAP:
                    line = "[CRASH] 进程被 SIGILL/SIGTRAP 终止（多为 Swift 的 fatalError、强解包 nil 或数组越界）：\n"
                case SIGSEGV, SIGBUS:
                    line = "[CRASH] 进程被 SIGSEGV/SIGBUS 终止（野指针或已释放对象）：\n"
                case SIGFPE:
                    line = "[CRASH] 进程被 SIGFPE 终止（除零或整数溢出）：\n"
                default:
                    line = "[CRASH] 进程被 SIGABRT 终止（未捕获异常或 abort，详情见上一行 ERROR）：\n"
                }
                line.withUTF8Buffer { buffer in
                    _ = Darwin.write(fd, buffer.baseAddress, buffer.count)
                }
                let frames = AppLogger.crashFrames
                backtrace_symbols_fd(frames, backtrace(frames, AppLogger.crashFrameCapacity), fd)
                signal(received, SIG_DFL)
                raise(received)
            }
        }
    }

    /// 崩溃面包屑用的日志 fd 与调用栈缓冲区：必须在信号到来之前就准备好。
    private static let crashFrameCapacity: Int32 = 48
    private static var crashLogFD: Int32 = -1
    private static let crashFrames = UnsafeMutablePointer<UnsafeMutableRawPointer?>
        .allocate(capacity: Int(crashFrameCapacity))

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
