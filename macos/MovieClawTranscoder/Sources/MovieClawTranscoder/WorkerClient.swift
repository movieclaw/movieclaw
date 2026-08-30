import Foundation

/// Worker 控制面 actor。
///
/// WebSocket、任务表和 ffmpeg 生命周期都在这个 actor 内串行化；菜单栏只消费
/// statuses 流，因此 UI 卡顿不会影响心跳或分片上传。
actor WorkerClient {
    nonisolated let statuses: AsyncStream<WorkerStatus>

    private let statusContinuation: AsyncStream<WorkerStatus>.Continuation
    private let configuration: WorkerConfiguration
    private let capabilities: WorkerCapabilities
    private var socket: URLSessionWebSocketTask?
    private var jobs: [String: JobExecution] = [:]
    private var uploadProxies: [String: ArtifactUploadProxy] = [:]
    private var jobAttempts: [String: String] = [:]
    /// 任务的展示名（服务端下发的源文件名），只用于菜单栏显示。
    private var jobNames: [String: String] = [:]
    private var lastProgressSent: [String: Date] = [:]
    private var currentProgress: [String: JobProgress] = [:]
    private var state: WorkerConnectionState = .stopped
    private var lastError: String?
    private var draining = false
    private var stopRequested = false
    /// 本轮连接是否收到过 worker.accepted。用来区分「连上后掉线」和「压根连不上」：
    /// 前者退避应从头开始，后者才该继续指数退避。
    private var handshakeCompleted = false
    /// 最近一次收到 NAS 消息的时间（含心跳 ack），用于判定半开连接。
    private var lastServerMessageAt = Date()

    /// 心跳间隔；服务端的离线判定窗口是它的三倍，留足丢包余量。
    private static let heartbeatIntervalNanoseconds: UInt64 = 15_000_000_000
    /// 多久没收到 NAS 任何消息就认定链路已死。与服务端 WORKER_IDLE_TIMEOUT_S
    /// 保持一致，避免两边对「这条连接还活着吗」给出相反的答案。
    private static let serverSilenceTimeout: TimeInterval = 45

    init(configuration: WorkerConfiguration, capabilities: WorkerCapabilities) {
        let stream = AsyncStream<WorkerStatus>.makeStream(
            of: WorkerStatus.self,
            bufferingPolicy: .bufferingNewest(32)
        )
        self.statuses = stream.stream
        self.statusContinuation = stream.continuation
        self.configuration = configuration
        self.capabilities = capabilities
    }

    func runForever() async {
        stopRequested = false
        publish(.starting, message: "Worker 正在启动")
        var retryDelay: UInt64 = 1_000_000_000
        while !Task.isCancelled && !stopRequested {
            publish(.connecting, message: "正在连接 NAS")
            do {
                try await runConnection()
                retryDelay = 1_000_000_000
            } catch {
                if Task.isCancelled || stopRequested { break }
                let message = sanitized(error.localizedDescription)
                lastError = message
                AppLogger.shared.warning("NAS 控制连接断开：\(message)", secret: configuration.workerToken)
                publish(.reconnecting, message: message, error: message)
                // 已经握手成功过的连接掉线，说明地址和令牌都是对的，只是链路断了：
                // 退避要从头开始，否则一条挂了几小时的连接断开后会直接按上次遗留
                // 的 30 秒等待，白白多离线半分钟。连不上的情况仍然继续指数退避。
                if handshakeCompleted {
                    retryDelay = 1_000_000_000
                }
            }
            stopAllJobs()
            guard !Task.isCancelled && !stopRequested else { break }
            let seconds = retryDelay / 1_000_000_000
            publish(.reconnecting, message: "\(seconds) 秒后重连")
            do {
                try await Task.sleep(nanoseconds: retryDelay)
            } catch {
                break
            }
            retryDelay = min(retryDelay * 2, 30_000_000_000)
        }
        stopAllJobs()
        publish(.stopped, message: "Worker 已停止")
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

    func setDraining(_ value: Bool) async {
        draining = value
        if socket != nil {
            try? await send(["type": value ? "worker.draining" : "worker.ready"])
        }
        publish(value ? .draining : (jobs.isEmpty ? .ready : .busy), message: value ? "暂停接收新任务" : "恢复接收新任务")
    }

    private func runConnection() async throws {
        handshakeCompleted = false
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

        try await send([
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
            ],
        ])
        if draining {
            try await send(["type": "worker.draining"])
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
                // 服务端主动拒绝时（1008）带着可读的理由，比如「凭证已吊销」
                // 或「远程转码开关没打开」。URLSession 抛出来的是一句
                // 「Socket is not connected」，把用户真正需要的那句话丢了。
                throw Self.closeReasonError(from: socket) ?? error
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

    /// 把服务端的 WebSocket 关闭理由取出来变成可读错误。
    ///
    /// 只认策略性关闭（1008 policyViolation）——那是服务端「我不接受你」的
    /// 明确表态，理由由服务端写好；网络断开之类的关闭码没有这种文本，
    /// 交回原错误即可。
    private static func closeReasonError(from socket: URLSessionWebSocketTask) -> Error? {
        guard socket.closeCode == .policyViolation,
              let data = socket.closeReason,
              let reason = String(data: data, encoding: .utf8),
              !reason.isEmpty
        else { return nil }
        return ConfigurationError.message(reason)
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
            publish(draining ? .draining : .ready, message: "Worker 已连接到 NAS")
            AppLogger.shared.info("Worker 已连接到 NAS：\(configuration.workerID)")
        case "job.start":
            await startJob(message)
        case "job.stop":
            if let jobID = message["job_id"] as? String {
                // 先从槽位表移除，再请求进程退出。seek 重启会带 force，直接
                // 杀掉没有交付价值的旧轮次；普通 stop 仍允许 ffmpeg 优雅收尾。
                let job = jobs.removeValue(forKey: jobID)
                let uploadProxy = uploadProxies.removeValue(forKey: jobID)
                jobAttempts.removeValue(forKey: jobID)
                currentProgress.removeValue(forKey: jobID)
                lastProgressSent.removeValue(forKey: jobID)
                job?.stop(force: message["force"] as? Bool ?? false)
                uploadProxy?.stop()
                publishCurrent(message: "任务已停止")
            }
        case "job.pause":
            if let jobID = message["job_id"] as? String {
                jobs[jobID]?.pause()
                publish(.paused, message: "任务已暂停")
            }
        case "job.resume":
            if let jobID = message["job_id"] as? String {
                jobs[jobID]?.resume()
                publish(.busy, message: "任务已恢复")
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
                let proxy = try ArtifactUploadProxy(jobID: jobID, remoteBaseURL: remoteBaseURL)
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
        do {
            try await send(["type": "job.accepted", "job_id": jobID, "attempt_id": attemptID])
        } catch {
            execution.stop()
            uploadProxies.removeValue(forKey: jobID)?.stop()
            jobs.removeValue(forKey: jobID)
            jobAttempts.removeValue(forKey: jobID)
            return
        }
        publish(.busy, message: "任务已接收")

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
                uploadFailure: uploadFailure
            )
        }
    }

    private func reportProgress(jobID: String, progress: JobProgress) async {
        guard jobs[jobID] != nil else { return }
        currentProgress[jobID] = progress
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
        uploadFailure: String? = nil
    ) async {
        // 旧任务可能在同一个 ID 的 seek 新任务之后才退出，只有仍登记的那一
        // 个 execution 才能释放槽位和上报状态。
        guard jobs[jobID] === execution else { return }
        let succeeded = result.succeeded && uploadFailure == nil
        let failure = uploadFailure ?? result.error
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
            AppLogger.shared.warning("远程任务失败：job=\(jobID) error=\(error)", secret: configuration.workerToken)
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
        let currentJobID = jobs.keys.sorted().first
        statusContinuation.yield(
            WorkerStatus(
                state: effectiveState,
                message: message,
                workerID: configuration.workerID,
                activeJobs: jobs.count,
                maxJobs: configuration.maxJobs,
                currentJobID: currentJobID,
                currentJobName: currentJobID.flatMap { jobNames[$0] },
                currentProgress: currentJobID.flatMap { currentProgress[$0] },
                ffmpegVersion: capabilities.ffmpegVersion,
                encoders: capabilities.encoders,
                lastError: lastError,
                updatedAt: Date()
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

private extension URL {
    func withScheme(_ scheme: String) -> URL {
        guard var components = URLComponents(url: self, resolvingAgainstBaseURL: false) else {
            return self
        }
        components.scheme = scheme
        return components.url ?? self
    }
}
