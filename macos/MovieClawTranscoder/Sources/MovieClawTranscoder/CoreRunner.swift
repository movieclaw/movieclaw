import Darwin
import Foundation

/// 转码内核进程（`movieclaw-transcoder --core`）：连 NAS、管 ffmpeg，没有任何界面。
///
/// 由菜单栏 App 里的 ``CoreSupervisor`` 启动和看管，消息格式见 ``CoreCommand`` / ``CoreEvent``。
/// 启动顺序：
///
/// 1. 从标准输入读第一行配置（令牌走管道）；
/// 2. 清理上一个内核崩溃后留下的孤儿 ffmpeg（``OrphanSweeper``）；
/// 3. 检测 ffmpeg 能力，不可用就以 ``CoreExitCode/ffmpegUnusable`` 退出（重启也没用）；
/// 4. 跑 ``WorkerClient``，状态逐条写到标准输出，每 10 秒经 WorkerClient 报一次平安。
///
/// 界面进程退出时标准输入读到 EOF，内核优雅收尾后自行退出，不会留下没人管的内核。
enum CoreRunner {
    static func run() -> Never {
        AppLogger.processTag = "[内核] "
        // 界面进程没了之后再往标准输出写，别被 SIGPIPE 直接杀掉——按管道断开正常收尾
        signal(SIGPIPE, SIG_IGN)
        AppLogger.shared.info("转码内核已启动：pid=\(getpid())")

        let output = CoreOutput()
        let session = CoreSession(output: output)
        let (commands, continuation) = AsyncStream<CoreCommand?>.makeStream()
        // 按顺序一条条处理：先 configure 才能 setDraining，顺序乱了就会丢指令
        Task {
            for await command in commands {
                if let command {
                    await session.handle(command)
                } else {
                    await session.parentGone()
                }
            }
        }
        Thread.detachNewThread {
            while let line = readLine(strippingNewline: true) {
                guard let command = try? CoreLine.decode(CoreCommand.self, from: Data(line.utf8)) else {
                    AppLogger.shared.warning("转码内核收到无法识别的指令，已忽略")
                    continue
                }
                continuation.yield(command)
            }
            continuation.yield(nil)
        }
        dispatchMain()
    }
}

/// 往标准输出写事件。串行队列保证一行不会被另一行插断。
final class CoreOutput: @unchecked Sendable {
    private let queue = DispatchQueue(label: "movieclaw.core.output")

    func send(_ event: CoreEvent) {
        guard let data = try? CoreLine.encode(event) else { return }
        queue.async {
            // 用会抛错的新接口：管道断了（界面进程已退出）抛错而不是 ObjC 异常闪退
            try? FileHandle.standardOutput.write(contentsOf: data)
        }
    }

    /// 退出前把还没写出去的事件写完。
    func flush() {
        queue.sync {}
    }
}

private actor CoreSession {
    private let output: CoreOutput
    private var client: WorkerClient?
    private var configured = false
    /// configure 之前就收到的暂停接单（理论上不会，保险起见记住）。
    private var pendingDraining: Bool?

    init(output: CoreOutput) {
        self.output = output
    }

    func handle(_ command: CoreCommand) async {
        switch command {
        case let .configure(configuration):
            await start(configuration)
        case let .setDraining(value):
            if let client {
                await client.setDraining(value)
            } else {
                pendingDraining = value
            }
        case .reconnectNow:
            await client?.reconnectNow()
        case .shutdown:
            await shutdown(reason: "界面要求退出")
        }
    }

    func parentGone() async {
        await shutdown(reason: "界面进程已退出")
    }

    private func start(_ core: CoreConfiguration) async {
        guard !configured else { return }
        configured = true
        let configuration: WorkerConfiguration
        do {
            configuration = try core.workerConfiguration()
        } catch {
            AppLogger.shared.error("转码内核配置无效：\(error.localizedDescription)")
            finish(.invalidConfiguration)
        }

        let orphans = OrphanSweeper.sweep(ffmpegPath: configuration.ffmpegPath)
        if !orphans.isEmpty {
            AppLogger.shared.warning(
                "清理了上一个转码内核留下的 \(orphans.count) 个 ffmpeg 进程：\(orphans.map(String.init).joined(separator: ","))"
            )
        }

        let path = configuration.ffmpegPath
        let probe = await Task.detached(priority: .utility) {
            Result { try CapabilityProbe.run(ffmpegPath: path) }
        }.value
        let capabilities: WorkerCapabilities
        switch probe {
        case let .success(value):
            capabilities = value
        case let .failure(error):
            let message = LogSanitizer.redact(error.localizedDescription, secret: configuration.workerToken)
            AppLogger.shared.error("转码内核启动失败，ffmpeg 不可用：\(message)")
            output.send(.status(.offline(
                .error, message: message, workerID: configuration.workerID, maxJobs: configuration.maxJobs,
                error: message, problem: .ffmpegUnusable
            )))
            finish(.ffmpegUnusable)
        }

        let output = self.output
        let client = WorkerClient(configuration: configuration, capabilities: capabilities) { record in
            output.send(.jobEnded(record))
        }
        self.client = client
        if let pendingDraining {
            await client.setDraining(pendingDraining)
        }
        Task {
            for await status in client.statuses {
                output.send(.status(status))
            }
        }
        // 报平安要先经过 WorkerClient：它被卡死（某个同步调用堵住了 actor）时这里就报不出来，
        // 界面进程 30 秒收不到任何消息就会判定内核卡死、强制重启
        Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                if await client.ping() {
                    output.send(.alive)
                }
            }
        }
        Task {
            let exit = await client.runForever()
            switch exit {
            case .stopped: finish(.normal)
            case .ffmpegUnusable: finish(.ffmpegUnusable)
            }
        }
    }

    private func shutdown(reason: String) async {
        AppLogger.shared.info("转码内核退出：\(reason)")
        if let client {
            // 最多等 3 秒：给 NAS 道别、停掉 ffmpeg；卡住了也不能拖着不走
            let done = DoneFlag()
            Task {
                await client.stop()
                done.set()
            }
            let deadline = Date().addingTimeInterval(3)
            while !done.isSet, Date() < deadline {
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
        }
        finish(.normal)
    }

    private nonisolated func finish(_ code: CoreExitCode) -> Never {
        output.flush()
        exit(code.rawValue)
    }
}

/// 跨任务的「做完了」标记。
private final class DoneFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    var isSet: Bool {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    func set() {
        lock.lock()
        value = true
        lock.unlock()
    }
}
