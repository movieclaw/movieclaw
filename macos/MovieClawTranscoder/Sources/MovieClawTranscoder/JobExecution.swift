import Darwin
import Foundation

struct JobResult: Sendable {
    let succeeded: Bool
    let exitCode: Int32
    let error: String?
    let stderrTail: String
}

/// 一个远程任务对应一个 ffmpeg 进程；它只接触管道和网络 URL，不创建媒体文件。
final class JobExecution: @unchecked Sendable {
    private let ffmpegPath: String
    private let lock = NSLock()
    private var process: Process?
    private var stopRequested = false
    private var pauseRequested = false
    private let stderrTail = LockedTail(maxCharacters: 8_000)

    init(ffmpegPath: String) {
        self.ffmpegPath = ffmpegPath
    }

    func run(
        arguments: [String],
        onProgress: @escaping @Sendable (JobProgress) -> Void = { _ in }
    ) async -> JobResult {
        if isStopRequested() {
            return JobResult(
                succeeded: false,
                exitCode: -1,
                error: "任务在 ffmpeg 启动前已取消",
                stderrTail: stderrTail.value
            )
        }
        let process = Process()
        let stderr = Pipe()
        let stdout = Pipe()
        let progressParser = ProgressParser(onProgress: onProgress)
        process.executableURL = URL(fileURLWithPath: ffmpegPath)
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdout
        process.standardError = stderr
        stderr.fileHandleForReading.readabilityHandler = { [stderrTail] handle in
            let data = handle.availableData
            if !data.isEmpty {
                stderrTail.append(String(data: data, encoding: .utf8) ?? "")
            }
        }
        stdout.fileHandleForReading.readabilityHandler = { [progressParser] handle in
            let data = handle.availableData
            if !data.isEmpty {
                progressParser.append(data)
            }
        }

        do {
            try process.run()
        } catch {
            stderr.fileHandleForReading.readabilityHandler = nil
            stdout.fileHandleForReading.readabilityHandler = nil
            return JobResult(
                succeeded: false,
                exitCode: -1,
                error: "无法启动 ffmpeg：\(error.localizedDescription)",
                stderrTail: stderrTail.value
            )
        }
        setProcess(process)
        if isStopRequested() {
            process.terminate()
        } else if isPauseRequested() {
            kill(process.processIdentifier, SIGSTOP)
        }

        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                process.waitUntilExit()
                continuation.resume()
            }
        }
        stderr.fileHandleForReading.readabilityHandler = nil
        stdout.fileHandleForReading.readabilityHandler = nil
        // readabilityHandler 是异步回调，进程退出时管道里可能还留着最后一
        // 批 stderr。补读一次，避免真正的 HTTP/HLS 错误恰好落在日志尾部之外。
        let remainingStderr = stderr.fileHandleForReading.readDataToEndOfFile()
        if !remainingStderr.isEmpty {
            stderrTail.append(String(data: remainingStderr, encoding: .utf8) ?? "")
        }
        progressParser.finish()
        let exitCode = process.terminationStatus
        setProcess(nil)
        let succeeded = exitCode == 0
        return JobResult(
            succeeded: succeeded,
            exitCode: exitCode,
            error: succeeded ? nil : "ffmpeg 退出码：\(exitCode)",
            stderrTail: LogSanitizer.redact(stderrTail.value)
        )
    }

    func stop(force: Bool = false) {
        lock.lock()
        stopRequested = true
        let process = self.process
        lock.unlock()
        guard let process, process.isRunning else { return }
        if force {
            // seek 重启时旧分片已经不会交给播放器，直接杀掉进程可避免旧轮次
            // 与新轮次并发读取源文件、上传产物。上传端点使用临时文件+原子替换，
            // 所以被中断的分片不会污染可读目标。
            kill(process.processIdentifier, SIGKILL)
            return
        }
        // terminate() 负责正常收尾；HTTP 输出卡住时再用 SIGKILL 保证 Stop
        // 不会一直占着 Worker 槽位。不会触碰任何源文件或分片文件。
        process.terminate()
        let pid = process.processIdentifier
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 2) {
            if process.isRunning {
                kill(pid, SIGKILL)
            }
        }
    }

    func pause() {
        lock.lock()
        pauseRequested = true
        let process = self.process
        lock.unlock()
        guard let process, process.isRunning else { return }
        kill(process.processIdentifier, SIGSTOP)
    }

    func resume() {
        lock.lock()
        pauseRequested = false
        let process = self.process
        lock.unlock()
        guard let process, process.isRunning else { return }
        kill(process.processIdentifier, SIGCONT)
    }

    private func setProcess(_ process: Process?) {
        lock.lock()
        self.process = process
        lock.unlock()
    }

    private func isStopRequested() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopRequested
    }

    private func isPauseRequested() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return pauseRequested
    }
}

private final class ProgressParser: @unchecked Sendable {
    private let lock = NSLock()
    private let onProgress: @Sendable (JobProgress) -> Void
    private var buffer = Data()
    private var values: [String: String] = [:]

    init(onProgress: @escaping @Sendable (JobProgress) -> Void) {
        self.onProgress = onProgress
    }

    func append(_ data: Data) {
        lock.lock()
        buffer.append(data)
        let lines = buffer.split(separator: 10, omittingEmptySubsequences: false)
        if let last = lines.last, !last.isEmpty, data.last != 10 {
            buffer = Data(last)
        } else {
            buffer.removeAll(keepingCapacity: true)
        }
        for line in lines.dropLast() {
            consume(String(decoding: line, as: UTF8.self))
        }
        lock.unlock()
    }

    func finish() {
        lock.lock()
        if !buffer.isEmpty {
            consume(String(decoding: buffer, as: UTF8.self))
            buffer.removeAll(keepingCapacity: false)
        }
        lock.unlock()
    }

    private func consume(_ line: String) {
        guard let separator = line.firstIndex(of: "=") else { return }
        let key = String(line[..<separator])
        let value = String(line[line.index(after: separator)...])
        values[key] = value
        guard key == "progress" else { return }
        let milliseconds = Int64(values["out_time_ms"] ?? "")
        let progress = JobProgress(
            outTimeMS: milliseconds,
            speed: values["speed"],
            phase: value
        )
        onProgress(progress)
    }
}

private final class LockedTail: @unchecked Sendable {
    private let lock = NSLock()
    private let maxCharacters: Int
    private var text = ""

    init(maxCharacters: Int) {
        self.maxCharacters = maxCharacters
    }

    func append(_ value: String) {
        lock.lock()
        defer { lock.unlock() }
        text.append(value)
        if text.count > maxCharacters {
            text = String(text.suffix(maxCharacters))
        }
    }

    var value: String {
        lock.lock()
        defer { lock.unlock() }
        return text
    }
}
