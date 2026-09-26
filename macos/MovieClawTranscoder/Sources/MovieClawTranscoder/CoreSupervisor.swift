import Darwin
import Foundation

/// 界面进程里看管转码内核进程（``CoreRunner``）的「看管员」。
///
/// 内核出任何致命问题都由它兜住，菜单栏 App 本身不受影响：
///
/// - **崩溃**（被信号杀掉、非约定的退出码）：按 ``CrashPolicy`` 退避重启，1、2、4…秒，
///   最长 60 秒；10 分钟内崩溃满 5 次就停手，面板上摆出原因和「重试」。
/// - **卡死**：内核每 10 秒报一次平安（要经过它的 WorkerClient），35 秒没有任何消息就
///   强制结束、按崩溃处理。
/// - **内存失控**：内核自身占用超过 1 GB，等手上没有任务时重启一次（不计入崩溃）。
/// - **按设计退出**（ffmpeg 不可用、配置无效）：不重启——重启一万次也一样，
///   把原因交给面板，等用户处理。NAS 拒绝凭证不在此列：内核自己放慢重试，不退出。
///
/// 每次自动恢复都记一笔（``recoveries``），面板底部会说「今天自动恢复过 N 次」。
/// 一次自动恢复（内核崩溃 / 卡死 / 内存过高后被重启）。
struct CoreRecovery: Equatable {
    var at: Date
    var reason: String
}

@MainActor
final class CoreSupervisor {
    var onStatus: ((WorkerStatus) -> Void)?
    var onJobEnded: ((JobRecord) -> Void)?
    /// 自动恢复记录变了（面板底部的说明要跟着变）。
    var onRecoveriesChanged: (() -> Void)?

    /// 看管中：已经 start、还没被要求停下。包括「崩溃后等着重启」的那段时间。
    private(set) var isActive = false
    private(set) var recoveries: [CoreRecovery] = []
    /// 当前内核进程号（诊断信息用）。
    var corePID: pid_t? { process?.processIdentifier }

    static let hangTimeout: TimeInterval = 35
    static let memoryLimit: UInt64 = 1 << 30

    private var configuration: WorkerConfiguration?
    private var process: Process?
    private var input: FileHandle?
    private var outputBuffer = Data()
    private var lastMessageAt = Date()
    private var lastStatus: WorkerStatus?
    private var drainingForUpdate = false
    private var policy = CrashPolicy()
    private var restartTask: Task<Void, Never>?
    private var watchdog: Timer?
    /// 这一次退出是我们自己要的（停止、重启），不算崩溃。
    private var intentionalExit = false
    /// 被判定卡死后强制结束的，退出时的原因写「无响应」而不是「信号 9」。
    private var killedAsHung = false

    // MARK: - 对外

    func start(_ configuration: WorkerConfiguration) {
        self.configuration = configuration
        isActive = true
        policy.reset()
        restartTask?.cancel()
        restartTask = nil
        startWatchdog()
        spawn()
    }

    /// 优雅停下：先请内核跟 NAS 道别，最多等 5 秒，还没走就强制结束。
    func stop() async {
        isActive = false
        restartTask?.cancel()
        restartTask = nil
        watchdog?.invalidate()
        watchdog = nil
        guard let process, process.isRunning else {
            self.process = nil
            return
        }
        intentionalExit = true
        send(.shutdown)
        let deadline = Date().addingTimeInterval(5)
        while process.isRunning, Date() < deadline {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
        }
    }

    /// App 退出时同步收尾：来不及等道别，直接结束内核（内核读到 EOF 也会自己退）。
    func terminateNow() {
        isActive = false
        restartTask?.cancel()
        watchdog?.invalidate()
        if let process, process.isRunning {
            intentionalExit = true
            send(.shutdown)
            process.terminate()
        }
    }

    /// 更新 ffmpeg 前暂停接单。内核重启后也要保持，所以记下来、每次启动内核时重发。
    func setDraining(_ value: Bool) {
        drainingForUpdate = value
        send(.setDraining(value))
    }

    /// 睡眠唤醒、网络恢复：请内核立刻确认连接；内核正等着重启的话，直接提前重启。
    func reconnectNow() {
        if process?.isRunning == true {
            send(.reconnectNow)
        } else if isActive, restartTask != nil {
            restartTask?.cancel()
            restartTask = nil
            spawn()
        }
    }

    // MARK: - 启动与消息

    private func spawn() {
        guard isActive, let configuration else { return }
        let process = Process()
        process.executableURL = Bundle.main.executableURL
            ?? URL(fileURLWithPath: CommandLine.arguments[0])
        process.arguments = ["--core"]
        let stdin = Pipe()
        let stdout = Pipe()
        process.standardInput = stdin
        process.standardOutput = stdout
        // 内核自己写日志文件；stderr 里只可能有系统框架的噪音
        process.standardError = FileHandle.nullDevice
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                // 读到末尾（内核退出）：必须摘掉，否则这个回调会被反复空转调用、白占 CPU
                handle.readabilityHandler = nil
                return
            }
            Task { @MainActor [weak self] in
                self?.receive(data)
            }
        }
        process.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            let reason = finished.terminationReason
            Task { @MainActor [weak self] in
                self?.handleExit(process: finished, status: status, reason: reason)
            }
        }
        intentionalExit = false
        killedAsHung = false
        outputBuffer.removeAll()
        lastMessageAt = Date()
        do {
            try process.run()
        } catch {
            AppLogger.shared.error("无法启动转码内核：\(error.localizedDescription)")
            handleCrash(reason: "无法启动（\(error.localizedDescription)）")
            return
        }
        self.process = process
        input = stdin.fileHandleForWriting
        send(.configure(CoreConfiguration(configuration)))
        if drainingForUpdate {
            send(.setDraining(true))
        }
        AppLogger.shared.info("已启动转码内核：pid=\(process.processIdentifier)")
    }

    private func send(_ command: CoreCommand) {
        guard let input, let data = try? CoreLine.encode(command) else { return }
        // 内核已经退出时管道写不进去：抛错而不是 ObjC 异常闪退
        try? input.write(contentsOf: data)
    }

    private func receive(_ data: Data) {
        guard !data.isEmpty else { return }
        outputBuffer.append(data)
        for line in CoreLine.takeLines(from: &outputBuffer) {
            lastMessageAt = Date()
            guard let event = try? CoreLine.decode(CoreEvent.self, from: line) else { continue }
            switch event {
            case let .status(status):
                lastStatus = status
                // 连上 NAS 了：这次恢复算成功，崩溃计数不必清（10 分钟窗口自己会过期），
                // 但面板上的「正在恢复」要撤掉——内核发来的状态本来就不带这个
                onStatus?(status)
            case let .jobEnded(record):
                onJobEnded?(record)
            case .alive:
                break
            }
        }
    }

    // MARK: - 退出与重启

    private func handleExit(process finished: Process, status: Int32, reason: Process.TerminationReason) {
        guard finished === process else { return }
        process = nil
        input = nil
        if intentionalExit || !isActive {
            return
        }
        if reason == .exit, let code = CoreExitCode(rawValue: status) {
            switch code {
            case .normal:
                // 没人让它停它却正常退出了：按崩溃处理，别让 Worker 悄悄掉线
                handleCrash(reason: "意外退出")
            case .invalidConfiguration, .ffmpegUnusable:
                // 按设计退出：不重启。ffmpeg 不可用时内核退出前已经发过带原因的状态
                AppLogger.shared.warning("转码内核按设计退出（退出码 \(status)），不自动重启")
                isActive = false
                watchdog?.invalidate()
                watchdog = nil
                if code == .invalidConfiguration {
                    publishProblem(nil, message: "转码内核认为配置无效，请在设置里检查 NAS 地址和名称")
                } else if lastStatus?.problem != .ffmpegUnusable {
                    publishProblem(.ffmpegUnusable, message: "ffmpeg 不可用，转码内核已停止")
                }
            }
            return
        }
        let detail: String
        if killedAsHung {
            detail = "\(Int(Self.hangTimeout)) 秒无响应"
        } else if reason == .uncaughtSignal {
            detail = "被信号 \(status) 终止"
        } else {
            detail = "退出码 \(status)"
        }
        handleCrash(reason: detail)
    }

    private func handleCrash(reason: String) {
        recoveries.append(CoreRecovery(at: Date(), reason: reason))
        recoveries = recoveries.filter { Date().timeIntervalSince($0.at) < 86_400 }
        onRecoveriesChanged?()
        switch policy.recordCrash(at: Date()) {
        case let .restart(delay, attempt):
            AppLogger.shared.warning("转码内核异常（\(reason)），\(Int(delay)) 秒后自动重启（第 \(attempt) 次）")
            publishProblem(
                .coreRecovering(attempt: attempt),
                message: "转码内核刚才\(reason)，\(Int(delay)) 秒后自动重启（第 \(attempt) 次）",
                state: .reconnecting
            )
            restartTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                guard !Task.isCancelled else { return }
                self?.restartTask = nil
                self?.spawn()
            }
        case .giveUp:
            let message = "转码内核 \(Int(CrashPolicy.window / 60)) 分钟内崩溃了 \(CrashPolicy.limit) 次"
                + "（最近一次：\(reason)），已停止自动重启"
            AppLogger.shared.error(message)
            isActive = false
            watchdog?.invalidate()
            watchdog = nil
            publishProblem(.coreCrashLoop, message: message)
        }
    }

    private func publishProblem(_ problem: WorkerProblem?, message: String, state: WorkerConnectionState = .error) {
        let status = WorkerStatus.offline(
            state,
            message: message,
            workerID: configuration?.workerID ?? "-",
            maxJobs: configuration?.maxJobs ?? 1,
            ffmpegVersion: lastStatus?.ffmpegVersion ?? "-",
            error: message,
            problem: problem
        )
        lastStatus = status
        onStatus?(status)
    }

    // MARK: - 卡死与内存看门狗

    private func startWatchdog() {
        watchdog?.invalidate()
        let timer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.checkHealth() }
        }
        RunLoop.main.add(timer, forMode: .common)
        watchdog = timer
    }

    private func checkHealth() {
        guard isActive, let process, process.isRunning else { return }
        if Date().timeIntervalSince(lastMessageAt) > Self.hangTimeout {
            AppLogger.shared.warning("转码内核 \(Int(Self.hangTimeout)) 秒没有任何响应，判定卡死，强制结束后重启")
            killedAsHung = true
            kill(process.processIdentifier, SIGKILL)
            return
        }
        if (lastStatus?.activeJobs ?? 0) == 0, let footprint = Self.footprint(of: process.processIdentifier),
           footprint > Self.memoryLimit {
            AppLogger.shared.warning("转码内核占用内存 \(footprint >> 20) MB，超过上限，空闲时重启一次")
            recoveries.append(CoreRecovery(at: Date(), reason: "内存占用过高（\(footprint >> 20) MB）"))
            onRecoveriesChanged?()
            intentionalExit = true
            send(.shutdown)
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
                self?.spawn()
            }
        }
    }

    private static func footprint(of pid: pid_t) -> UInt64? {
        var info = rusage_info_v4()
        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
                proc_pid_rusage(pid, RUSAGE_INFO_V4, $0)
            }
        }
        return result == 0 ? info.ri_phys_footprint : nil
    }
}
