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
    private var lastProgressSent: [String: Date] = [:]
    private var currentProgress: [String: JobProgress] = [:]
    private var state: WorkerConnectionState = .stopped
    private var lastError: String?
    private var draining = false
    private var stopRequested = false

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
        request.setValue(configuration.workerToken, forHTTPHeaderField: "X-MovieClaw-Worker-Token")
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
            let message = try await socket.receive()
            guard case let .string(text) = message,
                  let data = text.data(using: .utf8),
                  let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                continue
            }
            await handle(object)
        }
    }

    private func heartbeatLoop() async {
        while !Task.isCancelled && !stopRequested {
            do {
                try await Task.sleep(nanoseconds: 15_000_000_000)
            } catch {
                return
            }
            guard !Task.isCancelled && !stopRequested else { return }
            try? await send(["type": "worker.heartbeat"])
        }
    }

    private func handle(_ message: [String: Any]) async {
        guard let type = message["type"] as? String else { return }
        switch type {
        case "worker.accepted":
            lastError = nil
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
