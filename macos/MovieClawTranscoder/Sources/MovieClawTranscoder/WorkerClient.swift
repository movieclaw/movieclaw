import Foundation
import Network

/// ``WorkerClient/runForever()`` 为什么结束了。
enum WorkerExit: Equatable {
    /// 被要求停下（断开连接、退出 App）。
    case stopped
    /// 熔断后自检发现 ffmpeg 用不了。
    case ffmpegUnusable
}

/// Worker 控制面 actor。
///
/// WebSocket、任务表和 ffmpeg 生命周期都在这个 actor 内串行化；菜单栏只消费
/// statuses 流，因此 UI 卡顿不会影响心跳或分片上传。
///
/// 菜单栏 App 里它跑在独立的转码内核进程中（见 ``CoreRunner``），这里自己也兜着几层
/// 容错（见 FaultTolerance.swift）：任务卡死看门狗、连续失败熔断、被 NAS 拒绝时按理由
/// 决定是停下还是慢慢重试、睡眠唤醒与网络恢复后立刻重连。
actor WorkerClient {
    nonisolated let statuses: AsyncStream<WorkerStatus>

    private let statusContinuation: AsyncStream<WorkerStatus>.Continuation
    private let configuration: WorkerConfiguration
    private let capabilities: WorkerCapabilities
    private var socket: URLSessionWebSocketTask?
    private var jobs: [String: JobExecution] = [:] {
        didSet { updateSleepPrevention() }
    }
    /// 有任务在跑时持有的系统活动令牌，阻止空闲睡眠与 App Nap。
    ///
    /// 空闲睡眠看的是键鼠有没有动，不看 CPU 忙不忙：没人碰的 Mac mini 在默认电源
    /// 设置下十来分钟就会睡过去，控制连接随之中断，NAS 把任务判失败，正在看的片子
    /// 当场断掉。领先量节流会把 ffmpeg 挂起很久，这段时间 App 自己也几乎不干活，
    /// 菜单栏 App 又没有可见窗口，正是 App Nap 的目标——心跳定时器一被拖慢，NAS
    /// 就会判离线。任务全部结束立即归还，平时不影响 Mac 正常睡眠。
    private var sleepActivity: NSObjectProtocol?
    private var uploadProxies: [String: ArtifactUploadProxy] = [:]
    private var jobAttempts: [String: String] = [:]
    /// 任务的展示名（服务端下发的源文件名），只用于菜单栏显示。
    private var jobNames: [String: String] = [:]
    private var lastProgressSent: [String: Date] = [:]
    private var currentProgress: [String: JobProgress] = [:]
    /// 每个任务的计时与片内进度起止，结束时据此记一笔（见 ``recordJob``）。
    private var tracks: [String: JobTrack] = [:]
    /// 被 NAS 暂停的任务（job.pause），面板上单独标出来。
    private var pausedJobs: Set<String> = []
    /// 任务结束时交出去的记录（菜单栏 App 记进本地的任务记录；无界面模式不需要，为 nil）。
    private let recordJob: (@Sendable (JobRecord) -> Void)?
    private var state: WorkerConnectionState = .stopped
    private var lastError: String?
    /// 暂停接单的原因。两个来源各自增删、互不干扰：只用一个布尔的话，熔断冷却结束会把
    /// 「正在等任务转完好更新 ffmpeg」的暂停一并解开。
    private var drainReasons: Set<DrainReason> = []
    private var draining: Bool { !drainReasons.isEmpty }
    /// 面板要专门说明的故障（见 ``WorkerProblem``）。
    private var problem: WorkerProblem?
    /// 连续失败熔断（见 ``FailureBreaker``）。
    private var breaker = FailureBreaker()
    /// 熔断后自检发现 ffmpeg 不可用时，runForever 以此结束。
    private var fatalExit: WorkerExit?
    /// 睡眠唤醒、网络恢复：退避等待中的话立刻结束等待。
    private var wakeRequested = false
    private var stopRequested = false
    /// 本轮连接是否收到过 worker.accepted。用来区分「连上后掉线」和「压根连不上」：
    /// 前者退避应从头开始，后者才该继续指数退避。
    private var handshakeCompleted = false
    /// 本轮连接是被握手看门狗断开的（见 ``handshakeTimeout``）。
    private var handshakeTimedOut = false
    /// 最近一次收到 NAS 消息的时间（含心跳 ack），用于判定半开连接。
    private var lastServerMessageAt = Date()

    /// 心跳间隔；服务端的离线判定窗口是它的三倍，留足丢包余量。
    private static let heartbeatIntervalNanoseconds: UInt64 = 15_000_000_000
    /// 多久没收到 NAS 任何消息就认定链路已死。与服务端 WORKER_IDLE_TIMEOUT_S
    /// 保持一致，避免两边对「这条连接还活着吗」给出相反的答案。
    private static let serverSilenceTimeout: TimeInterval = 45
    /// 从发起连接到收到 worker.accepted 最多等多久。心跳和上面的静默检测都在握手之后
    /// 才开始，握手这一段得单独兜住：服务端握手一成功就发关闭帧时（实测 Python
    /// websockets 库的服务端会这样），URLSession 的 send / receive 既不返回也不报错，
    /// 内核会永远挂在「正在连接」；反向代理接了升级请求、后端却没响应也一样。
    /// TCP 连不上（地址不通）也由它兜，比 URLSession 默认的 60 秒请求超时快。服务端等 hello
    /// 是 10 秒，这里留足余量。
    private static let handshakeTimeout: TimeInterval = 20

    private enum DrainReason: Hashable {
        /// 界面要更新 ffmpeg，等手上的任务转完。
        case update
        /// 连续失败熔断，冷却自检中。
        case cooldown
    }

    init(
        configuration: WorkerConfiguration,
        capabilities: WorkerCapabilities,
        recordJob: (@Sendable (JobRecord) -> Void)? = nil
    ) {
        let stream = AsyncStream<WorkerStatus>.makeStream(
            of: WorkerStatus.self,
            bufferingPolicy: .bufferingNewest(32)
        )
        self.statuses = stream.stream
        self.statusContinuation = stream.continuation
        self.configuration = configuration
        self.capabilities = capabilities
        self.recordJob = recordJob
    }

    @discardableResult
    func runForever() async -> WorkerExit {
        stopRequested = false
        fatalExit = nil
        publish(.starting, message: "Worker 正在启动")
        let watchdog = Task { [weak self] in await self?.watchdogLoop() }
        let pathMonitor = startPathMonitor()
        defer {
            watchdog.cancel()
            pathMonitor.cancel()
        }
        var retryDelay: TimeInterval = 1
        while !Task.isCancelled && !stopRequested {
            publish(.connecting, message: "正在连接 NAS")
            do {
                try await runConnection()
                retryDelay = 1
            } catch {
                if Task.isCancelled || stopRequested { break }
                let message = sanitized(error.localizedDescription)
                lastError = message
                AppLogger.shared.warning("NAS 控制连接断开：\(message)", secret: configuration.workerToken)
                // 已经握手成功过的连接掉线，说明地址和令牌都是对的，只是链路断了：
                // 退避要从头开始，否则一条挂了几小时的连接断开后会直接按上次遗留
                // 的 30 秒等待，白白多离线半分钟。连不上的情况仍然继续指数退避。
                if handshakeCompleted {
                    retryDelay = 1
                }
                // 面板上的「授权失效」「开关没开」只反映最近一次连接的结果：NAS 后来连不上了，
                // 就不该还挂着「开关没开」
                if problem == .authRejected || problem == .remoteDisabled {
                    problem = nil
                }
                switch (error as? NASRejectionError)?.kind {
                case .authRejected?:
                    // 凭证被吊销或失效：多半得重新配对，但不停下——NAS 从备份恢复之类的
                    // 情况下凭证会重新有效。放慢到每 5 分钟试一次（NAS 那边同一原因的
                    // 拒绝 10 分钟才记一行日志）；重新配对后内核带着新凭证重启，不用等
                    problem = .authRejected
                    retryDelay = 300
                case .remoteDisabled?:
                    // 等管理员去网页打开开关：每分钟问一次就够了
                    problem = .remoteDisabled
                    retryDelay = 60
                case .other?:
                    retryDelay = max(retryDelay, 60)
                case nil:
                    break
                }
                publish(.reconnecting, message: message, error: message)
            }
            stopAllJobs()
            guard !Task.isCancelled && !stopRequested else { break }
            publish(.reconnecting, message: "\(Int(retryDelay)) 秒后重连")
            guard await sleepUnlessWoken(seconds: retryDelay) else { break }
            if problem != .remoteDisabled {
                retryDelay = min(retryDelay * 2, 30)
            }
        }
        stopAllJobs()
        if let fatalExit {
            publish(.error, message: lastError ?? "Worker 已停止", error: lastError)
            return fatalExit
        }
        publish(.stopped, message: "Worker 已停止")
        return .stopped
    }

    /// 退避等待。被 ``reconnectNow()`` 叫醒或被停止时提前结束；返回 false 表示该退出了。
    private func sleepUnlessWoken(seconds: TimeInterval) async -> Bool {
        wakeRequested = false
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if wakeRequested || stopRequested || Task.isCancelled { break }
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
        return !(stopRequested || Task.isCancelled)
    }

    /// 睡眠唤醒、网络恢复后调用：别等退避，立刻确认连接。
    ///
    /// - 正在退避等待：立刻重连；
    /// - 连着：发一个心跳，5 秒内 NAS 没任何回应就断开重连——睡眠后 TCP 多半已经死了，
    ///   但 receive() 要等很久才会发现（半开连接），这 45 秒里 NAS 早把我们判离线了。
    func reconnectNow() {
        guard !stopRequested else { return }
        guard socket != nil, handshakeCompleted else {
            wakeRequested = true
            return
        }
        let probeStarted = Date()
        Task { [weak self] in
            try? await self?.send(["type": "worker.heartbeat"])
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            await self?.dropIfSilent(since: probeStarted)
        }
    }

    private func dropIfSilent(since probeStarted: Date) {
        guard socket != nil, lastServerMessageAt < probeStarted else { return }
        AppLogger.shared.warning("唤醒后 NAS 5 秒没有回应，连接多半已失效，立刻重连")
        socket?.cancel(with: .goingAway, reason: nil)
    }

    /// 网络从断开变为可用时立刻重连（换 Wi-Fi、网线插回、VPN 切换）。
    private func startPathMonitor() -> NWPathMonitor {
        let monitor = NWPathMonitor()
        // 回调只在下面这个串行队列上执行，上一次的状态记在盒子里就够了
        final class LastState: @unchecked Sendable { var satisfied = true }
        let last = LastState()
        monitor.pathUpdateHandler = { [weak self] path in
            let satisfied = path.status == .satisfied
            defer { last.satisfied = satisfied }
            guard satisfied, !last.satisfied else { return }
            AppLogger.shared.info("网络恢复，立刻重连 NAS")
            Task { await self?.reconnectNow() }
        }
        monitor.start(queue: DispatchQueue(label: "movieclaw.worker.path"))
        return monitor
    }

    /// 报平安用：能走到这里说明 actor 没被卡死（见 ``CoreRunner``）。
    func ping() -> Bool {
        true
    }

    func stop() async {
        stopRequested = true
        if socket != nil {
            try? await send(["type": "worker.goodbye"])
            socket?.cancel(with: .goingAway, reason: nil)
        }
        stopAllJobs()
        publish(.stopped, message: "Worker 已停止")
    }

    /// 界面要更新 ffmpeg 前暂停接单（手上的任务照常转完）。
    func setDraining(_ value: Bool) async {
        await updateDrain(.update, on: value)
        publishCurrent(message: value ? "暂停接收新任务" : "恢复接收新任务")
    }

    private func updateDrain(_ reason: DrainReason, on: Bool) async {
        let before = draining
        if on {
            drainReasons.insert(reason)
        } else {
            drainReasons.remove(reason)
        }
        guard draining != before, socket != nil else { return }
        try? await send(["type": draining ? "worker.draining" : "worker.ready"])
    }

    private func runConnection() async throws {
        handshakeCompleted = false
        handshakeTimedOut = false
        lastServerMessageAt = Date()
        var endpoint = configuration.nasURL
            .appendingPathComponent("api")
            .appendingPathComponent("v1")
            .appendingPathComponent("transcode-worker")
            .appendingPathComponent("ws")
        switch endpoint.scheme?.lowercased() {
        case "https":
            endpoint = endpoint.withScheme("wss")
        case "http":
            endpoint = endpoint.withScheme("ws")
        default:
            throw ConfigurationError.message("NAS 地址协议无效，仅支持 HTTP 或 HTTPS")
        }
        var request = URLRequest(url: endpoint)
        // 标准 Authorization: Bearer，与 CLI 走同一个验签入口
        // （docs/design/device-auth.md §5.4）。放 Header 而不是查询参数，
        // 避免长期令牌进反向代理访问日志与监控 URL。
        request.setValue("Bearer \(configuration.workerToken)", forHTTPHeaderField: "Authorization")
        let session = URLSession(configuration: .ephemeral)
        let socket = session.webSocketTask(with: request)
        self.socket = socket
        socket.resume()
        defer {
            socket.cancel(with: .goingAway, reason: nil)
            session.invalidateAndCancel()
            self.socket = nil
        }
        let handshakeGuard = Task { [weak self] in
            do {
                try await Task.sleep(nanoseconds: UInt64(Self.handshakeTimeout * 1_000_000_000))
            } catch {
                return
            }
            await self?.abortStalledHandshake(socket)
        }
        defer { handshakeGuard.cancel() }

        let hello: [String: Any] = [
            "type": "worker.hello",
            "protocol_version": BuildInfo.protocolVersion,
            "worker_version": BuildInfo.version,
            "worker_id": configuration.workerID,
            "draining": draining,
            "capabilities": [
                "platform": "macOS",
                "arch": "arm64",
                "ffmpeg_version": capabilities.ffmpegVersion,
                "encoders": capabilities.encoders,
                "backends": capabilities.backends,
                "max_jobs": configuration.maxJobs,
                // 旧版服务端忽略这个字段；新版只把 TS 分片任务派给声明了 mpegts 的 Worker
                "segment_types": ArtifactUploadProxy.supportedSegmentTypes,
                // 能接收 job.playback（观众播放位置），面板上显示「看到 25:10 / 1:52:10」
                "playback_progress": true,
                // 能读原盘：源是 NAS 下发的 ffconcat 清单，各段剪辑一个 HTTP 地址
                "disc_sources": true,
                // NAS 按这两项装命令：能硬解的走 GPU（缩放、HDR 色调映射都在 Metal 上），
                // 解不了的编码（VC-1、WMV……）CPU 软解、编码仍用 VideoToolbox
                "hw_decoders": capabilities.hwDecoders,
                "filters": capabilities.filters,
            ],
        ]
        do {
            try await send(hello)
            if draining {
                try await send(["type": "worker.draining"])
            }
        } catch {
            throw connectionError(error, socket: socket)
        }
        let heartbeat = Task { [weak self] in
            await self?.heartbeatLoop()
        }
        defer { heartbeat.cancel() }

        while !Task.isCancelled && !stopRequested {
            let message: URLSessionWebSocketTask.Message
            do {
                message = try await socket.receive()
            } catch {
                throw connectionError(error, socket: socket)
            }
            // 任何一条消息都算链路活着，心跳 ack 也不例外
            lastServerMessageAt = Date()
            guard case let .string(text) = message,
                  let data = text.data(using: .utf8),
                  let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                continue
            }
            await handle(object)
        }
    }

    /// 握手看门狗到点：还没收到 worker.accepted 就断开，卡住的 send / receive 随之返回。
    private func abortStalledHandshake(_ candidate: URLSessionWebSocketTask) {
        guard socket === candidate, !handshakeCompleted else { return }
        handshakeTimedOut = true
        candidate.cancel(with: .goingAway, reason: nil)
    }

    /// 把连接失败翻译成用户看得懂、知道下一步的原因。URLSession 自己的说法是
    /// 「There was a bad response from the server」「Socket is not connected」这类英文，
    /// 真正有用的信息（NAS 的拒绝理由、HTTP 状态码、超时）全丢了。
    private func connectionError(_ error: Error, socket: URLSessionWebSocketTask) -> Error {
        if handshakeTimedOut {
            return ConfigurationError.message(
                "连接 NAS 超时：\(Int(Self.handshakeTimeout)) 秒内没有完成握手，稍后自动重连"
            )
        }
        // 策略性关闭（1008）是服务端「我不接受你」的明确表态，理由由服务端写好，
        // 比如「凭证已吊销」或「远程转码开关没打开」；网络断开之类的关闭码没有这种文本
        if socket.closeCode == .policyViolation,
           let data = socket.closeReason,
           let reason = String(data: data, encoding: .utf8),
           !reason.isEmpty {
            return NASRejectionError(reason: reason)
        }
        // 握手阶段就被 HTTP 状态码拒绝（旧版服务端、反向代理）
        if let status = (socket.response as? HTTPURLResponse)?.statusCode, status != 101 {
            return Self.handshakeStatusError(status)
        }
        return error
    }

    /// 握手被 HTTP 状态码拒绝时的说法。
    static func handshakeStatusError(_ status: Int) -> Error {
        switch status {
        case 403:
            // 旧版服务端在 accept 之前就关连接，uvicorn 只回一个空的 403、理由丢了：
            // 凭证失效和开关没开分不出来，两种都得说
            return NASRejectionError(
                reason: "NAS 拒绝了连接（HTTP 403）：请确认网页「应用 → 远程转码」已经打开；"
                    + "已经打开的话，说明这台 Mac 的授权已失效，请在网页「设置 → 设备」重新配对"
            )
        case 404:
            return NASRejectionError(
                reason: "NAS 上找不到远程转码接口（HTTP 404）：地址可能填错了，或 movieclaw 版本太旧"
            )
        case 500...:
            // 后端重启（比如 NAS 正在更新）时反向代理回 502 / 503，等一下就好，照常退避
            return ConfigurationError.message("NAS 暂时不可用（HTTP \(status)），稍后自动重连")
        default:
            return NASRejectionError(reason: "NAS 拒绝了连接（HTTP \(status)）")
        }
    }

    private func heartbeatLoop() async {
        while !Task.isCancelled && !stopRequested {
            do {
                try await Task.sleep(nanoseconds: Self.heartbeatIntervalNanoseconds)
            } catch {
                return
            }
            guard !Task.isCancelled && !stopRequested else { return }
            // 半开连接（Mac 休眠唤醒、NAS 掉电、路由器换 NAT 映射）下，socket
            // 的 receive() 会一直挂到 TCP 自己放弃，可能好几分钟。服务端 45 秒
            // 就把我们判离线不再派单了，这段时间里 Worker 却以为自己在线、也
            // 不会重连。这里用同样的窗口主动断开，把重连交给外层循环。
            if Date().timeIntervalSince(lastServerMessageAt) > Self.serverSilenceTimeout {
                let seconds = Int(Self.serverSilenceTimeout)
                AppLogger.shared.warning("NAS 超过 \(seconds) 秒没有任何响应，主动断开重连")
                socket?.cancel(with: .goingAway, reason: nil)
                return
            }
            try? await send(["type": "worker.heartbeat"])
        }
    }

    private func handle(_ message: [String: Any]) async {
        guard let type = message["type"] as? String else { return }
        switch type {
        case "worker.accepted":
            lastError = nil
            handshakeCompleted = true
            if problem == .remoteDisabled || problem == .authRejected {
                problem = nil
            }
            publish(draining ? .draining : .ready, message: "Worker 已连接到 NAS")
            AppLogger.shared.info("Worker 已连接到 NAS：\(configuration.workerID)")
        case "job.start":
            await startJob(message)
        case "job.stop":
            if let jobID = message["job_id"] as? String {
                // 先从槽位表移除，再请求进程退出。seek 重启会带 force，直接
                // 杀掉没有交付价值的旧轮次；普通 stop 仍允许 ffmpeg 优雅收尾。
                let force = message["force"] as? Bool ?? false
                if jobs[jobID] != nil {
                    recordEnd(jobID: jobID, outcome: .stopped)
                }
                let job = jobs.removeValue(forKey: jobID)
                let uploadProxy = uploadProxies.removeValue(forKey: jobID)
                jobAttempts.removeValue(forKey: jobID)
                currentProgress.removeValue(forKey: jobID)
                lastProgressSent.removeValue(forKey: jobID)
                // 这一行必须有：被 NAS 停掉的任务不会走 finish() 上报，日志里
                // 只剩「上传代理已停止」，看不出是谁停的、为什么停。issue #286
                // 里 NAS 每 2~3 秒杀一次任务，Worker 日志却完全无法自证清白。
                AppLogger.shared.info(
                    "NAS 要求停止任务：job=\(jobID) force=\(force) 在跑=\(job != nil)"
                )
                job?.stop(force: force)
                uploadProxy?.stop()
                publishCurrent(message: "任务已停止")
            }
        case "job.pause":
            if let jobID = message["job_id"] as? String {
                jobs[jobID]?.pause()
                if jobs[jobID] != nil { pausedJobs.insert(jobID) }
                publish(.paused, message: "任务已暂停")
            }
        case "job.resume":
            if let jobID = message["job_id"] as? String {
                jobs[jobID]?.resume()
                pausedJobs.remove(jobID)
                // 暂停期间本来就没有进度，看门狗从恢复这一刻重新计时
                tracks[jobID]?.lastProgressAt = Date()
                publish(.busy, message: "任务已恢复")
            }
        case "job.playback":
            if let jobID = message["job_id"] as? String, tracks[jobID] != nil {
                func milliseconds(_ key: String) -> Int64? {
                    (message[key] as? NSNumber).map { $0.int64Value }
                }
                tracks[jobID]?.playback = JobPlayback(
                    positionMS: milliseconds("position_ms"),
                    viewerPaused: message["viewer_paused"] as? Bool ?? false,
                    durationMS: milliseconds("duration_ms"),
                    preparedMS: milliseconds("prepared_ms")
                )
                publish(pausedJobs.isEmpty ? .busy : .paused, message: "播放位置更新")
            }
        case "worker.heartbeat.ack":
            break
        default:
            AppLogger.shared.warning("忽略 NAS 未知控制消息：\(type)")
        }
    }

    private func startJob(_ message: [String: Any]) async {
        guard let jobID = message["job_id"] as? String,
              let arguments = message["ffmpeg_args"] as? [String]
        else {
            await sendFailure(jobID: message["job_id"] as? String ?? "unknown", error: "任务缺少 job_id 或 ffmpeg_args")
            return
        }
        let attemptID = message["attempt_id"] as? String ?? jobID
        // 旧版服务端不带这个字段，缺了就退回显示 job id
        if let name = message["display_name"] as? String, !name.isEmpty {
            jobNames[jobID] = name
        }
        if draining {
            await sendFailure(jobID: jobID, attemptID: attemptID, error: "Worker 正在排空，不接受新任务")
            return
        }
        if let existingAttempt = jobAttempts[jobID] {
            if existingAttempt == attemptID {
                // 同一任务的 start 重传是幂等操作，只补发 accepted，不重启 ffmpeg。
                try? await send(["type": "job.accepted", "job_id": jobID, "attempt_id": attemptID])
                return
            }
            recordEnd(jobID: jobID, outcome: .stopped)
            jobs.removeValue(forKey: jobID)?.stop()
            uploadProxies.removeValue(forKey: jobID)?.stop()
            jobAttempts.removeValue(forKey: jobID)
            currentProgress.removeValue(forKey: jobID)
        }
        guard jobs.count < configuration.maxJobs else {
            await sendFailure(jobID: jobID, attemptID: attemptID, error: "Worker 并发已满")
            return
        }
        let execution = JobExecution(ffmpegPath: configuration.ffmpegPath)
        var ffmpegArguments = arguments
        var uploadProxy: ArtifactUploadProxy?
        if let remoteBaseURL = ArtifactUploadProxy.remoteArtifactBaseURL(from: arguments) {
            do {
                // ffmpeg 不看上传响应码，产物丢了只能由代理报上来：白名单拒收说明这个
                // 任务注定交不出产物，立即杀掉让终态带着原因回 NAS；重试用尽则告诉
                // NAS 哪一片没了，由它补片重启——ffmpeg 自己不会回头补写。
                let proxy = try ArtifactUploadProxy(
                    jobID: jobID,
                    remoteBaseURL: remoteBaseURL
                ) { [weak self, execution] event in
                    switch event {
                    case .rejected:
                        execution.stop(force: true)
                    case let .abandoned(name, status, reason):
                        Task { [weak self] in
                            await self?.reportArtifactFailure(
                                jobID: jobID,
                                attemptID: attemptID,
                                name: name,
                                status: status,
                                reason: reason
                            )
                        }
                    }
                }
                let localBaseURL = try await proxy.start()
                ffmpegArguments = proxy.rewrite(arguments: arguments, localBaseURL: localBaseURL)
                uploadProxy = proxy
                uploadProxies[jobID] = proxy
                AppLogger.shared.info(
                    "远程任务启用内存上传代理：job=\(jobID) NAS=\(remoteBaseURL.host ?? "unknown")"
                )
            } catch {
                let message = sanitized("无法启动远程产物上传代理：\(error.localizedDescription)")
                AppLogger.shared.error("远程任务无法启动：job=\(jobID) error=\(message)")
                await sendFailure(jobID: jobID, attemptID: attemptID, error: message)
                return
            }
        } else {
            // 兼容旧服务端或非 HLS 任务；当前远程播放任务应始终命中代理。
            AppLogger.shared.warning("远程任务未找到产物地址，将由 ffmpeg 直接处理：job=\(jobID)")
        }
        jobs[jobID] = execution
        jobAttempts[jobID] = attemptID
        tracks[jobID] = JobTrack(
            startedAt: Date(),
            videoEncoder: Self.videoEncoder(in: arguments),
            startOffsetMS: Self.seekOffsetMS(in: arguments)
        )
        do {
            try await send(["type": "job.accepted", "job_id": jobID, "attempt_id": attemptID])
        } catch {
            execution.stop()
            uploadProxies.removeValue(forKey: jobID)?.stop()
            jobs.removeValue(forKey: jobID)
            jobAttempts.removeValue(forKey: jobID)
            tracks.removeValue(forKey: jobID)
            return
        }
        publish(.busy, message: "任务已接收")
        AppLogger.shared.info(
            "开始转码任务：job=\(jobID) 片名=\(jobNames[jobID] ?? "-") " +
            "起点=\(Self.formatStart(message["start_ms"])) 分片=\(Self.segmentType(in: arguments))"
        )

        let activeFFmpegArguments = ffmpegArguments
        let activeUploadProxy = uploadProxy
        Task { [weak self, execution, activeFFmpegArguments, activeUploadProxy] in
            let result = await execution.run(arguments: activeFFmpegArguments) { [weak self] progress in
                Task { [weak self] in
                    await self?.reportProgress(jobID: jobID, progress: progress)
                }
            }
            if let activeUploadProxy {
                await activeUploadProxy.drainPendingUploads()
            }
            let uploadFailure = activeUploadProxy?.failureDescription
            activeUploadProxy?.stop()
            await self?.finish(
                jobID: jobID,
                execution: execution,
                result: result,
                uploadFailure: uploadFailure,
                arguments: activeFFmpegArguments
            )
        }
    }

    /// 告诉 NAS 某个产物重试用尽仍没传上去，由它从这一片补片重启。
    /// 旧版服务端不认识这条消息，只会忽略——行为与之前一致。
    private func reportArtifactFailure(
        jobID: String,
        attemptID: String,
        name: String,
        status: Int,
        reason: String
    ) async {
        // 已被 NAS 叫停的任务（seek 旧轮次）丢片无所谓，不报
        guard jobs[jobID] != nil else { return }
        try? await send([
            "type": "job.artifact_failed",
            "job_id": jobID,
            "attempt_id": attemptID,
            "name": name,
            "status": status,
            "error": sanitized(reason),
        ])
    }

    /// 任务日志里的起转位置（时:分:秒）。
    private static func formatStart(_ value: Any?) -> String {
        guard let milliseconds = value as? Int else { return "-" }
        let seconds = milliseconds / 1000
        return String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
    }

    /// ffmpeg 参数里的 `-hls_segment_type`（fmp4 / mpegts），排查分片类型问题用。
    private static func segmentType(in arguments: [String]) -> String {
        guard let index = arguments.firstIndex(of: "-hls_segment_type"),
              index + 1 < arguments.count
        else { return "-" }
        return arguments[index + 1]
    }

    /// 按当前任务表拿住或归还防睡眠令牌（见 ``sleepActivity``）。
    private func updateSleepPrevention() {
        if jobs.isEmpty {
            guard let activity = sleepActivity else { return }
            ProcessInfo.processInfo.endActivity(activity)
            sleepActivity = nil
            AppLogger.shared.info("转码任务已全部结束，恢复系统空闲睡眠")
        } else if sleepActivity == nil {
            sleepActivity = ProcessInfo.processInfo.beginActivity(
                options: [.userInitiated],
                reason: "MovieClaw 正在转码，播放器在等分片"
            )
            AppLogger.shared.info("有转码任务在进行，已阻止系统空闲睡眠与 App Nap")
        }
    }

    private func reportProgress(jobID: String, progress: JobProgress) async {
        guard jobs[jobID] != nil else { return }
        currentProgress[jobID] = progress
        tracks[jobID]?.lastProgressAt = Date()
        if let outTimeMS = progress.outTimeMS {
            tracks[jobID]?.observe(outTimeMS)
        }
        let now = Date()
        let shouldSend = progress.phase == "end"
            || now.timeIntervalSince(lastProgressSent[jobID] ?? .distantPast) >= 0.8
        guard shouldSend else {
            publishCurrent(message: "任务转码中")
            return
        }
        lastProgressSent[jobID] = now
        var payload: [String: Any] = [
            "type": "job.progress",
            "job_id": jobID,
        ]
        if let outTimeMS = progress.outTimeMS { payload["out_time_ms"] = outTimeMS }
        if let speed = progress.speed { payload["speed"] = speed }
        if let phase = progress.phase { payload["phase"] = phase }
        try? await send(payload)
        publishCurrent(message: "任务转码中")
    }

    private func finish(
        jobID: String,
        execution: JobExecution,
        result: JobResult,
        uploadFailure: String? = nil,
        arguments: [String] = []
    ) async {
        // 旧任务可能在同一个 ID 的 seek 新任务之后才退出，只有仍登记的那一
        // 个 execution 才能释放槽位和上报状态。被 NAS job.stop 摘掉的任务也
        // 走到这里：留一行日志说明退出结果被有意忽略，排查时才能把
        // 「NAS 要求停止」和 ffmpeg 的真实退出对上。
        guard jobs[jobID] === execution else {
            AppLogger.shared.info(
                "任务已不在登记表中，忽略其退出结果：job=\(jobID) exit_code=\(result.exitCode)"
            )
            return
        }
        let watchdogReason = tracks[jobID]?.watchdogReason
        let succeeded = result.succeeded && uploadFailure == nil && watchdogReason == nil
        let failure = watchdogReason ?? uploadFailure ?? result.error
        recordEnd(
            jobID: jobID,
            outcome: succeeded ? .finished : .failed,
            error: succeeded ? nil : sanitized(failure ?? "ffmpeg 转码失败")
        )
        let attemptID = jobAttempts.removeValue(forKey: jobID) ?? jobID
        jobNames.removeValue(forKey: jobID)
        jobs.removeValue(forKey: jobID)
        uploadProxies.removeValue(forKey: jobID)?.stop()
        currentProgress.removeValue(forKey: jobID)
        lastProgressSent.removeValue(forKey: jobID)
        if succeeded {
            try? await send([
                "type": "job.finished",
                "job_id": jobID,
                "attempt_id": attemptID,
                "exit_code": result.exitCode,
            ])
            // ffmpeg 可能以 0 退出，但在 stderr 中留下 HTTP 上传、输入流或
            // HLS muxer 的警告。成功任务也保留这段诊断信息，便于定位「任务完成
            // 但 init.mp4/playlist 没有上传」这类播放器只显示缓存的问题。
            let diagnostic = sanitized(result.stderrTail)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !diagnostic.isEmpty {
                let diagnosticTail = String(diagnostic.suffix(2_000))
                AppLogger.shared.warning(
                    "远程任务完成但 ffmpeg 输出了诊断信息：job=\(jobID) stderr=\(diagnosticTail)",
                    secret: configuration.workerToken
                )
            }
            AppLogger.shared.info("远程任务完成：job=\(jobID)")
        } else {
            let error = sanitized(failure ?? "ffmpeg 转码失败")
            try? await send([
                "type": "job.failed",
                "job_id": jobID,
                "attempt_id": attemptID,
                "exit_code": result.exitCode,
                "error": error,
                "stderr_tail": sanitized(result.stderrTail),
            ])
            // 失败时把 stderr 末尾和完整参数（令牌已脱敏）一起落进日志：用户反馈时
            // 附上这一段，就能在另一台 Mac 上照着参数原样复现
            let stderrTail = result.stderrTail
                .split(separator: "\n")
                .suffix(10)
                .joined(separator: "\n")
            AppLogger.shared.warning(
                "远程任务失败：job=\(jobID) error=\(error)\n" +
                "ffmpeg 退出码：\(result.exitCode)\n" +
                "ffmpeg stderr 末尾：\n\(stderrTail.isEmpty ? "（无输出）" : stderrTail)\n" +
                "ffmpeg 参数：\(arguments.joined(separator: " "))",
                secret: configuration.workerToken
            )
            lastError = error
        }
        publishCurrent(message: succeeded ? "任务完成" : "任务失败")
    }

    private func sendFailure(jobID: String, attemptID: String? = nil, error: String) async {
        var message: [String: Any] = [
            "type": "job.failed",
            "job_id": jobID,
            "error": error,
        ]
        if let attemptID { message["attempt_id"] = attemptID }
        try? await send(message)
    }

    private func stopAllJobs() {
        for jobID in jobs.keys {
            recordEnd(jobID: jobID, outcome: .stopped)
        }
        for job in jobs.values {
            job.stop()
        }
        for proxy in uploadProxies.values {
            proxy.stop()
        }
        jobs.removeAll()
        uploadProxies.removeAll()
        jobAttempts.removeAll()
        jobNames.removeAll()
        currentProgress.removeAll()
        lastProgressSent.removeAll()
        tracks.removeAll()
        pausedJobs.removeAll()
    }

    /// 任务结束（转完、失败、被叫停、断线）时往本地记录里记一笔。
    /// 必须在清掉 `jobNames` / `tracks` 之前调用。
    private func recordEnd(jobID: String, outcome: JobRecord.Outcome, error: String? = nil) {
        pausedJobs.remove(jobID)
        guard let track = tracks.removeValue(forKey: jobID) else { return }
        let now = Date()
        let ranFor = now.timeIntervalSince(track.startedAt)
        recordJob?(JobRecord(
            id: jobID,
            name: jobNames[jobID],
            outcome: outcome,
            error: error,
            endedAt: now,
            mediaMS: track.mediaMS,
            elapsed: ranFor,
            watchedMS: track.playback?.positionMS
        ))
        if breaker.record(outcome, ranFor: ranFor, at: now) {
            Task { await self.tripBreaker(lastFailure: error) }
        }
    }

    // MARK: - 容错：卡死看门狗、连续失败熔断

    /// 每 10 秒看一遍在跑的任务，卡住的（见 ``JobWatchdog``）强制结束。结束后照常走
    /// finish()：失败原因写看门狗那句话，NAS 据此重试或降档。
    private func watchdogLoop() async {
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            let now = Date()
            for (jobID, execution) in jobs {
                guard let track = tracks[jobID], track.watchdogReason == nil,
                      let reason = JobWatchdog.verdict(
                          startedAt: track.startedAt,
                          lastProgressAt: track.lastProgressAt,
                          paused: pausedJobs.contains(jobID),
                          now: now
                      )
                else { continue }
                tracks[jobID]?.watchdogReason = reason
                AppLogger.shared.warning("任务卡死，强制结束：job=\(jobID) \(reason)")
                execution.stop(force: true)
            }
        }
    }

    /// 连续失败熔断：暂停接单、自检 ffmpeg；自检通过则冷却期满自动恢复，不通过就停下
    /// 等用户处理（ffmpeg 坏了继续接单只会让每次播放都先失败一遍）。
    private func tripBreaker(lastFailure: String?) async {
        guard !drainReasons.contains(.cooldown) else { return }
        let failures = breaker.quickFailures.count
        let until = Date().addingTimeInterval(FailureBreaker.cooldown)
        problem = .cooldown(until: until, failures: failures)
        await updateDrain(.cooldown, on: true)
        let reason = lastFailure.map { "（最近一次：\($0)）" } ?? ""
        AppLogger.shared.warning("连续 \(failures) 个任务刚开始就失败\(reason)，暂停接单并自检 ffmpeg")
        publishCurrent(message: "连续 \(failures) 个任务刚开始就失败，暂停接单并自检")

        let path = configuration.ffmpegPath
        let healthy = await Task.detached(priority: .utility) {
            (try? CapabilityProbe.run(ffmpegPath: path)) != nil
        }.value
        guard healthy else {
            let message = "自检发现 ffmpeg 不可用（\(path)），已停止接单。请在设置的「转码」页重新下载或换一个 ffmpeg。"
            AppLogger.shared.error(message)
            lastError = message
            problem = .ffmpegUnusable
            fatalExit = .ffmpegUnusable
            stopRequested = true
            wakeRequested = true
            socket?.cancel(with: .goingAway, reason: nil)
            return
        }
        AppLogger.shared.info("ffmpeg 自检通过，\(Int(FailureBreaker.cooldown / 60)) 分钟后恢复接单")
        try? await Task.sleep(nanoseconds: UInt64(FailureBreaker.cooldown * 1_000_000_000))
        await endCooldown()
    }

    private func endCooldown() async {
        guard drainReasons.contains(.cooldown) else { return }
        breaker.reset()
        if case .cooldown = problem {
            problem = nil
        }
        await updateDrain(.cooldown, on: false)
        AppLogger.shared.info("熔断冷却结束，恢复接单")
        publishCurrent(message: "恢复接单")
    }

    /// 这一轮从片子的哪个位置起转：第一个 `-i` 之前的 `-ss`（输入侧 seek，秒，可带小数）。
    /// 输出侧的 `-ss` 意思不同（丢弃开头），不算。没有就是从头转。
    static func seekOffsetMS(in arguments: [String]) -> Int64 {
        let inputIndex = arguments.firstIndex(of: "-i") ?? arguments.endIndex
        guard let index = arguments[..<inputIndex].firstIndex(of: "-ss"),
              index + 1 < inputIndex,
              let seconds = Double(arguments[index + 1]), seconds > 0
        else { return 0 }
        return Int64((seconds * 1_000).rounded())
    }

    /// ffmpeg 参数里的视频编码器（`-c:v` / `-codec:v` / `-vcodec` 的值）。
    static func videoEncoder(in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(where: { ["-c:v", "-codec:v", "-vcodec"].contains($0) }),
              index + 1 < arguments.count
        else { return nil }
        return arguments[index + 1]
    }

    private func publishCurrent(message: String) {
        publish(jobs.isEmpty ? (draining ? .draining : .ready) : .busy, message: message)
    }

    private func publish(_ requestedState: WorkerConnectionState, message: String, error: String? = nil) {
        let effectiveState: WorkerConnectionState
        if requestedState != .stopped && requestedState != .error && draining {
            effectiveState = .draining
        } else if requestedState == .ready && !jobs.isEmpty {
            effectiveState = .busy
        } else {
            effectiveState = requestedState
        }
        state = effectiveState
        if let error { lastError = error }
        let running = jobs.keys.sorted().map { jobID in
            RunningJob(
                id: jobID,
                name: jobNames[jobID],
                progress: currentProgress[jobID],
                startedAt: tracks[jobID]?.startedAt ?? Date(),
                videoEncoder: tracks[jobID]?.videoEncoder,
                startOffsetMS: tracks[jobID]?.startOffsetMS ?? 0,
                paused: pausedJobs.contains(jobID),
                playback: tracks[jobID]?.playback
            )
        }
        statusContinuation.yield(
            WorkerStatus(
                state: effectiveState,
                message: message,
                workerID: configuration.workerID,
                maxJobs: configuration.maxJobs,
                jobs: running,
                ffmpegVersion: capabilities.ffmpegVersion,
                encoders: capabilities.encoders,
                lastError: lastError,
                updatedAt: Date(),
                problem: problem
            )
        )
    }

    private func sanitized(_ text: String) -> String {
        LogSanitizer.redact(text, secret: configuration.workerToken)
    }

    private func send(_ object: [String: Any]) async throws {
        guard let socket,
              JSONSerialization.isValidJSONObject(object)
        else {
            throw ConfigurationError.message("WebSocket 尚未连接或消息格式无效")
        }
        let data = try JSONSerialization.data(withJSONObject: object)
        guard let text = String(data: data, encoding: .utf8) else {
            throw ConfigurationError.message("无法编码控制消息")
        }
        try await socket.send(.string(text))
    }
}

/// NAS 拒绝连接时的错误：1008 关闭帧里服务端写好的理由（见 ``NASRejection``），
/// 或者握手被 HTTP 状态码拒绝时我们替它说的话（见 ``WorkerClient/handshakeStatusError(_:)``）。
struct NASRejectionError: Error, LocalizedError {
    let reason: String
    var kind: NASRejection { NASRejection(reason: reason) }
    var errorDescription: String? { reason }
}

/// 一个任务的计时与片内进度起止。
///
/// 转出的片长取「最后一次进度 − 第一次进度」而不是最后一次进度本身：从中间
/// 起转（续播、拖动）时 ffmpeg 报的位置可能带着起点偏移，直接拿来算会把没转
/// 的那一段也算进去。
private struct JobTrack {
    let startedAt: Date
    let videoEncoder: String?
    let startOffsetMS: Int64
    /// NAS 最近一次推来的观众播放位置。
    var playback: JobPlayback?
    /// 最近一次收到 ffmpeg 进度的时间（看门狗用，暂停恢复时重置）。
    var lastProgressAt: Date?
    /// 被看门狗判定卡死、强制结束时的原因；finish() 用它代替「退出码 -9」上报。
    var watchdogReason: String?
    private var firstOutMS: Int64?
    private var lastOutMS: Int64?

    init(startedAt: Date, videoEncoder: String?, startOffsetMS: Int64) {
        self.startedAt = startedAt
        self.videoEncoder = videoEncoder
        self.startOffsetMS = startOffsetMS
    }

    mutating func observe(_ outTimeMS: Int64) {
        if firstOutMS == nil { firstOutMS = outTimeMS }
        lastOutMS = outTimeMS
    }

    var mediaMS: Int64 {
        max(0, (lastOutMS ?? 0) - (firstOutMS ?? 0))
    }
}

private extension URL {
    func withScheme(_ scheme: String) -> URL {
        guard var components = URLComponents(url: self, resolvingAgainstBaseURL: false) else {
            return self
        }
        components.scheme = scheme
        return components.url ?? self
    }
}
