import SwiftUI

/// 字幕行里的「AI 生成字幕」入口（对应 Web `components/subtitle-gen-panel.tsx`）。
///
/// 交互分成两个明确阶段（与 Web 一致）：
/// 1. 点击后先做**预检**（不调用 LLM）：在弹层里讲清参考字幕、目标语言、输出文件与预计成本；
/// 2. 用户确认后才启动后台任务。
///
/// 任务状态的数据源：Web 用全站 JobsProvider（首屏快照 + `/jobs/stream` SSE 即时刷新 +
/// 15 秒低频轮询兜底）。App 没有全站任务仓库，这里就按同一口径只盯「这个文件的字幕生成任务」：
/// SSE 收到 `ready`/`job` 事件后合并成一次查询（120ms 去抖），另挂 15 秒兜底轮询。
/// 因此 Web、CLI、Agent 发起的任务在这里呈现完全一致。
///
/// 可见条件：仅管理员、且文件在盘（Web `!isAdmin || file.missing` 时不渲染）；
/// 未接入任何 AI 模型时，按钮换成「去接入」引导（Web LlmCapabilityGate）。
struct TrackSubtitleGenButton: View {
    let file: API.LibraryFileView
    /// 产物落盘后回调（详情页借此重拉字幕清单，新字幕立刻可见）
    var onChanged: () async -> Void = {}

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router
    @State private var model = TrackGenModel()

    var body: some View {
        if permissions.isAdmin, !file.missing {
            entry
                .task(id: file.id) {
                    model.onChanged = onChanged
                    await model.refreshJob(api: api, fileId: file.id)
                    await model.watchJobs(api: api, fileId: file.id)
                }
                .task { await model.checkLlm(api: api) }
                .polling(every: 15) { await model.refreshJob(api: api, fileId: file.id) }
                // 用布尔而非 item 绑定：状态弹层里点「重新预检」会原地切到预检模式，
                // item 身份一变 SwiftUI 就会关掉重开，onDismiss 会把刚发出的预检掐掉
                .sheet(
                    isPresented: Binding(get: { model.mode != nil }, set: { if !$0 { model.mode = nil } }),
                    onDismiss: { model.stopPreview() }
                ) {
                    TrackGenSheet(model: model, file: file)
                }
        }
    }

    /// 入口：运行中 / 未完成时总是显示状态按钮；空闲时受 LLM 能力门禁约束
    @ViewBuilder
    private var entry: some View {
        if model.running || model.hasTerminalIssue {
            badgeButton
        } else {
            switch model.llm {
            case .checking:
                // 检查中不短暂露出触发按钮（同 Web）
                EmptyView()
            case .missing:
                Button {
                    router.open(.settingsSection(.llm))
                } label: {
                    Text("接入 AI 模型后即可解锁生成字幕能力。\(Text("去接入").foregroundStyle(Theme.accent))")
                        .font(.caption)
                        .foregroundStyle(Theme.warning)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Theme.warning.opacity(0.1), in: .rect(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Theme.warning.opacity(0.3)))
                }
                .buttonStyle(.plain)
            case .configured, .unavailable:
                // 探测失败按 fail-open 处理，交给后端预检兜底
                badgeButton
            }
        }
    }

    /// 与语言芯片同一副骨架，只靠描边把「可点的动作」与「只读的标签」区分开
    private var badgeButton: some View {
        let generated = file.subtitleStreams.contains { $0.external && TrackGenText.isAiSubtitle($0.fileName) }
        let style = model.badgeStyle(generated: generated)
        let text = model.badgeLabel(generated: generated)
        return Button {
            model.openAction(api: api, fileId: file.id)
        } label: {
            HStack(spacing: 6) {
                if model.running {
                    Image(systemName: "circle.fill")
                        .font(.system(size: 6))
                        .symbolEffect(.pulse)
                } else {
                    Image(systemName: "wand.and.stars").font(.caption)
                }
                Text(text).lineLimit(1)
            }
            .font(.caption.weight(.medium).monospacedDigit())
            .foregroundStyle(style.foreground)
            .padding(.horizontal, 10)
            .frame(height: 32)
            .background(style.background, in: .rect(cornerRadius: 7))
            .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(style.border))
        }
        .buttonStyle(.plain)
        .disabled(model.previewing)
        .opacity(model.previewing ? 0.5 : 1)
        .accessibilityLabel(model.running ? "\(text)，\(model.job?.progress.message.nonEmpty ?? "正在运行")" : text)
    }
}

// MARK: - 状态模型

/// 生成面板的全部状态与动作。按钮与弹层共用一份，弹层关掉再打开时语言选择不丢（同 Web）。
@Observable
private final class TrackGenModel {
    enum Mode: String, Identifiable {
        case preview, status
        var id: String { rawValue }
    }

    enum LlmState { case checking, configured, missing, unavailable }

    struct BadgeStyle {
        var foreground: Color
        var background: Color
        var border: Color
    }

    // 输出选择
    var targetLanguage = "chs"
    var bilingual = false
    var secondaryLanguage = "eng"
    var sourceCandidateKey: String?
    var pgsOcrLanguage = ""

    // 预检
    var preview: API.GenPreviewView?
    var previewing = false
    /// 内封轨正在后台抽取时的等待文案；非空即「还没有结论，不是出错了」
    var pendingNotice: String?
    var requestError: String?

    // 弹层与动作
    var mode: Mode?
    var starting = false
    var stopping = false
    var agentStarting = false

    // 任务
    var job: API.JobView?
    var llm: LlmState = .checking
    var onChanged: () async -> Void = {}

    @ObservationIgnored private var previewTask: Task<Void, Never>?
    @ObservationIgnored private var refreshSeq = 0
    @ObservationIgnored private var refreshScheduled = false

    static let runningStatuses: Set<String> = ["queued", "running", "retry_wait", "cancelling", "waiting"]

    var running: Bool { job.map { Self.runningStatuses.contains($0.status) } ?? false }
    var hasTerminalIssue: Bool { ["blocked", "failed", "cancelled"].contains(job?.status ?? "") }
    var jobSucceeded: Bool { job?.status == "succeeded" }
    var progress: TrackGenProgress? { job.map(TrackGenProgress.init) }

    // MARK: 徽章

    func badgeStyle(generated: Bool) -> BadgeStyle {
        if previewing { return Self.idleStyle }
        if running {
            return BadgeStyle(foreground: Theme.info, background: Theme.info.opacity(0.12), border: Theme.info.opacity(0.3))
        }
        if generated || jobSucceeded { return Self.idleStyle }
        if hasTerminalIssue {
            let red = Color(red: 1, green: 0.62, blue: 0.62)
            return BadgeStyle(foreground: Color(red: 1, green: 0.71, blue: 0.71), background: red.opacity(0.07), border: red.opacity(0.25))
        }
        return Self.idleStyle
    }

    private static let idleStyle = BadgeStyle(
        foreground: Color.white.opacity(0.75), background: .clear, border: Color.white.opacity(0.16)
    )

    func badgeLabel(generated: Bool) -> String {
        // 已经生成过也不改口称「新建 AI 版本」：用户要的答案始终是同一句「让 AI 给这个片子做字幕」
        var text = "AI 生成字幕"
        if previewing {
            text = "正在检查"
        } else if running {
            text = TrackGenText.runningBadgeText(progress)
        } else if generated || jobSucceeded {
            // 生成成功过的文件即便带着历史失败记录，也不该再顶着红色的未完成态
        } else if hasTerminalIssue {
            text = "AI 未完成"
        }
        return running ? "\(activeOutputLabel) · \(text)" : text
    }

    /// 运行中 / 结束态报的目标输出：以任务入参为准（Agent、CLI 发起的任务也看得到）
    var activeOutputLabel: String {
        let target = progress?.targetLanguage.flatMap { $0.isEmpty ? nil : $0 } ?? targetLanguage
        return TrackGenText.outputLabel(target, progress?.secondaryLanguage)
    }

    var currentOutputLabel: String {
        TrackGenText.outputLabel(targetLanguage, bilingual ? secondaryLanguage : nil)
    }

    // MARK: 打开

    func openAction(api: APIClient, fileId: Int) {
        if running || hasTerminalIssue {
            mode = .status
            return
        }
        loadPreview(api: api, fileId: fileId)
    }

    // MARK: 预检

    /// 收掉在途预检：中断请求、停止 pending 轮询。换轨、改语言、关弹层都要真的把上一条掐掉——
    /// 否则拨一下「双语」开关，后端就会对同一个大文件再起一个 ffmpeg（issue #432）。
    /// 后端的抽取任务不受影响，会继续抽完落缓存，下次秒开。
    func stopPreview() {
        previewTask?.cancel()
        previewTask = nil
        previewing = false
        pendingNotice = nil
    }

    func loadPreview(api: APIClient, fileId: Int, sourceKey: String? = nil) {
        previewTask?.cancel()
        let target = targetLanguage
        let secondary = bilingual ? secondaryLanguage : nil
        mode = .preview
        preview = nil
        pgsOcrLanguage = ""
        requestError = nil
        pendingNotice = nil
        previewing = true
        previewTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    let result = try await api.trackRowsGenerationPreview(
                        fileId: fileId, targetLanguage: target, secondaryLanguage: secondary, sourceCandidateKey: sourceKey
                    )
                    guard let self, !Task.isCancelled else { return }
                    if let pending = result.pending {
                        // 内封轨还在后台抽取：保持「正在检查」并按后端给的间隔重拉。
                        // 这里**不能**写入 preview——那份快照里 chosen/blocker 都是空的，
                        // 渲染出来就成了「这份片源没有参考字幕」，与事实相反。
                        self.pendingNotice = pending.message
                        try? await Task.sleep(for: .milliseconds(max(1000, pending.retryAfterMs)))
                        continue
                    }
                    self.pendingNotice = nil
                    self.preview = result
                    self.sourceCandidateKey = result.selectedSourceKey
                    self.pgsOcrLanguage = result.pgsConversion?.ocrLanguage ?? ""
                    self.previewing = false
                    return
                } catch {
                    // 主动取消（换轨 / 关弹层 / 离开页面）不是错误，别弹红框
                    guard let self, !Task.isCancelled, !(error is CancellationError) else { return }
                    self.pendingNotice = nil
                    self.requestError = error.localizedDescription
                    self.previewing = false
                    return
                }
            }
        }
    }

    func changeTarget(_ target: String, api: APIClient, fileId: Int) {
        targetLanguage = target
        secondaryLanguage = TrackGenText.nextSecondary(target: target, current: secondaryLanguage)
        loadPreview(api: api, fileId: fileId)
    }

    func changeSecondary(_ secondary: String, api: APIClient, fileId: Int) {
        secondaryLanguage = secondary
        loadPreview(api: api, fileId: fileId)
    }

    func changeBilingual(_ enabled: Bool, api: APIClient, fileId: Int) {
        bilingual = enabled
        secondaryLanguage = TrackGenText.nextSecondary(target: targetLanguage, current: secondaryLanguage)
        loadPreview(api: api, fileId: fileId)
    }

    // MARK: 预检结论的派生量

    var chosen: API.SourceCandidateView? {
        preview?.candidates.first { TrackGenText.candidateKey($0) == preview?.chosenKey }
    }

    var selectedCandidate: API.SourceCandidateView? {
        preview?.candidates.first { TrackGenText.candidateKey($0) == preview?.selectedSourceKey }
    }

    var canPreparePgs: Bool {
        preview?.blocker?.code == "pgs_conversion_required" && preview?.pgsConversion?.available == true
    }

    var canConvertPgs: Bool {
        let languageReady = !(preview?.pgsConversion?.languageConfirmationRequired ?? false) || !pgsOcrLanguage.isEmpty
        return canPreparePgs && languageReady
    }

    /// 预检没通过（且不是可转换的 PGS）
    var blockedWithoutPgs: Bool { preview?.blocker != nil && !canPreparePgs }

    // MARK: 启动 / 停止

    func start(api: APIClient, fileId: Int) async {
        starting = true
        requestError = nil
        defer { starting = false }
        let convertPgs = canPreparePgs
        do {
            let started = try await api.librarySubtitlesGenerate(fileId: fileId, body: API.GenStartPayload(
                targetLanguage: targetLanguage,
                secondaryLanguage: bilingual ? secondaryLanguage : nil,
                sourceCandidateKey: sourceCandidateKey,
                convertPgs: convertPgs,
                pgsOcrLanguage: convertPgs && !pgsOcrLanguage.isEmpty ? pgsOcrLanguage : nil
            ))
            apply(started)
            mode = nil
        } catch is CancellationError {
        } catch {
            // 确认到真正入队之间文件可能变化，错误留在弹层里让用户看清
            requestError = error.localizedDescription
        }
    }

    func stop(api: APIClient) async {
        guard let jobId = job?.id else { return }
        stopping = true
        requestError = nil
        defer { stopping = false }
        do {
            apply(try await api.jobsCancel(jobId: jobId).job)
        } catch is CancellationError {
        } catch {
            requestError = error.localizedDescription
        }
    }

    // MARK: 交给 Agent

    /// 「交给 Agent 处理」必须把用户已经做出的选择一起带走（issue #433）：
    /// 除了人话的「期望输出」，再给一行可直接执行的 CLI 参数，别让 Agent 按默认参数重跑。
    /// 成功后返回新会话 id，由调用方跳转。
    func handOffToAgent(api: APIClient, file: API.LibraryFileView, reason: String) async -> String? {
        agentStarting = true
        requestError = nil
        defer { agentStarting = false }
        let conversion = preview?.pgsConversion
        let jobLanguage = mode == .status ? progress?.targetLanguage : nil
        let target = jobLanguage ?? targetLanguage
        let secondary = jobLanguage != nil ? progress?.secondaryLanguage : (bilingual ? secondaryLanguage : nil)
        let sourceKey = jobLanguage != nil ? progress?.sourceCandidateKey : sourceCandidateKey
        var params = "对应参数：--target-language \(target)"
        if let secondary { params += " --secondary-language \(secondary)" }
        if let sourceKey { params += " --source-candidate-key \(sourceKey)" }
        var lines: [String] = [
            "请帮我处理 MovieClaw 的 AI 字幕生成问题。",
            "文件：\(file.fileName)",
            "文件台账 ID：\(file.id)",
            "文件路径：\(file.filePath)",
            "期望输出：\(TrackGenText.outputLabel(target, secondary))",
            params,
            "当前问题：\(reason)",
        ]
        if let conversion {
            lines.append("预检环境：\(conversion.platform) \(conversion.architecture)" + (conversion.engine.map { " · \($0)" } ?? " · 未找到可用识别引擎"))
            if !conversion.message.isEmpty { lines.append("预检诊断：\(conversion.message)") }
        }
        lines += (preview?.blocker?.suggestions ?? []).map { "已有建议：\($0)" }
        lines.append("请先判断原因；如果能通过 MovieClaw 的工具安全解决，请按上面的「对应参数」直接执行（不要换回默认参数），否则给出明确的操作步骤。不要修改影片原文件。")
        do {
            let accepted = try await api.sessionStart(body: API.SessionStartPayload(content: lines.joined(separator: "\n")))
            mode = nil
            return accepted.sessionId
        } catch is CancellationError {
            return nil
        } catch {
            requestError = "无法启动 Agent：\(error.localizedDescription)"
            return nil
        }
    }

    // MARK: 任务跟踪

    /// 合并一条任务快照；只在亲眼看到同一任务从活跃态变为成功时通知详情页重拉
    /// （初次读到历史成功任务不触发多余请求）。
    func apply(_ next: API.JobView?) {
        if let previous = job, let next, previous.id == next.id,
           Self.runningStatuses.contains(previous.status), next.status == "succeeded" {
            Task { await onChanged() }
        }
        job = next
    }

    /// 查这个文件最近的一条字幕生成任务（取 updated_at 最新，同 Web latestFor）
    func refreshJob(api: APIClient, fileId: Int) async {
        refreshSeq += 1
        let seq = refreshSeq
        do {
            let list = try await api.jobsList(
                jobType: "subtitle.generate", resourceType: "library_file", resourceId: String(fileId), limit: 5
            )
            guard seq == refreshSeq else { return }
            let latest = list.items.max { lhs, rhs in
                (Formatters.date(lhs.updatedAt) ?? .distantPast) < (Formatters.date(rhs.updatedAt) ?? .distantPast)
            }
            apply(latest)
        } catch {
            // 瞬时断线保留最近快照；SSE 重连或下一轮轮询会自动校准
        }
    }

    /// 订阅任务中心 SSE：`ready` / `job` 事件只当「有变化」的信号，合并成一次查询。
    /// 流断开（反代不支持流式、网络抖动）3 秒后重连；15 秒兜底轮询另由视图挂载。
    func watchJobs(api: APIClient, fileId: Int) async {
        while !Task.isCancelled {
            do {
                for try await event in api.events("/jobs/stream") where event.event == "ready" || event.event == "job" {
                    scheduleRefresh(api: api, fileId: fileId)
                }
            } catch {}
            try? await Task.sleep(for: .seconds(3))
        }
    }

    private func scheduleRefresh(api: APIClient, fileId: Int) {
        guard !refreshScheduled else { return }
        refreshScheduled = true
        Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(120))
            guard let self else { return }
            self.refreshScheduled = false
            await self.refreshJob(api: api, fileId: fileId)
        }
    }

    func checkLlm(api: APIClient) async {
        do {
            llm = try await api.llmProvidersList().isEmpty ? .missing : .configured
        } catch is CancellationError {
        } catch {
            // 探测接口异常不应误锁 AI 功能，保留服务端提交时的错误兜底
            llm = .unavailable
        }
    }
}

// MARK: - 进度快照

/// 从任务 progress.details / usage / input_data 里拍平出进度展示所需字段（同 Web subtitleProgress）
private struct TrackGenProgress {
    var phase: String
    var message: String
    var percent: Double?
    var doneBlocks: Int
    var totalBlocks: Int
    var doneEvents: Int
    var totalEvents: Int
    var activeBlocks: [Int]
    var parallelism: Int
    var oldestActiveSeconds: Int
    var validationRetries: Int
    var rateLimitCount: Int
    var modelRequests: Int
    var modelTokens: Int
    var usesOcr: Bool
    var targetLanguage: String?
    var secondaryLanguage: String?
    var sourceCandidateKey: String?
    var elapsedSeconds: Int

    init(job: API.JobView) {
        let details = job.progress.details
        func number(_ dict: [String: API.JSONValue], _ key: String) -> Int {
            guard let value = dict[key], let double = value.doubleValue, double.isFinite else { return 0 }
            if case .string = value { return 0 }
            return Int(double)
        }
        func string(_ key: String) -> String? {
            if let value = details[key], case let .string(text) = value, !text.isEmpty { return text }
            if let value = job.inputData[key], case let .string(text) = value { return text }
            return nil
        }
        phase = job.progress.phase
        message = job.progress.message
        percent = job.progress.percent
        doneBlocks = number(details, "done_blocks")
        totalBlocks = number(details, "total_blocks")
        doneEvents = number(details, "done_events")
        totalEvents = number(details, "total_events")
        activeBlocks = (details["active_blocks"]?.arrayValue ?? []).compactMap { value in
            switch value {
            case let .int(number): number
            case let .double(number): Int(number)
            default: nil
            }
        }
        parallelism = number(details, "parallelism")
        oldestActiveSeconds = number(details, "oldest_active_seconds")
        validationRetries = number(details, "validation_retries")
        rateLimitCount = number(details, "rate_limit_count")
        modelRequests = number(job.usage, "request_count")
        modelTokens = number(job.usage, "total_tokens")
        usesOcr = details["uses_ocr"]?.boolValue == true
        targetLanguage = string("target_language")
        secondaryLanguage = string("secondary_language")
        sourceCandidateKey = string("source_candidate_key")
        if let started = Formatters.date(job.startedAt) {
            elapsedSeconds = max(0, Int(Date.now.timeIntervalSince(started)))
        } else {
            elapsedSeconds = 0
        }
    }

    var percentValue: Int? { percent.map { min(100, Int($0.rounded(.down))) } }

    var stageIndex: Int {
        TrackGenText.stages.firstIndex { $0.phases.contains(phase) } ?? 0
    }
}

// MARK: - 文案与格式化（逐字照搬 Web）

private enum TrackGenText {
    static let outputLanguages: [(token: String, label: String)] = [
        ("chs", "简体中文"), ("cht", "繁体中文"), ("eng", "英语"), ("jpn", "日语"),
        ("kor", "韩语"), ("fre", "法语"), ("ger", "德语"), ("spa", "西班牙语"),
        ("ita", "意大利语"), ("por", "葡萄牙语"), ("rus", "俄语"), ("tha", "泰语"),
    ]

    static let stages: [(phases: [String], label: String)] = [
        (["preparing", "ocr", "syncing"], "准备并检查字幕"),
        (["glossary"], "统一人名与术语"),
        (["translating"], "翻译对白"),
        (["validating", "compressing"], "检查字幕质量"),
        (["writing", "refreshing"], "保存并更新字幕"),
    ]

    static func outputLanguageLabel(_ token: String?) -> String {
        outputLanguages.first { $0.token == token }?.label ?? token ?? "字幕"
    }

    static func outputLabel(_ target: String, _ secondary: String?) -> String {
        let primary = outputLanguageLabel(target)
        guard let secondary else { return primary }
        return "\(primary) + \(outputLanguageLabel(secondary))双语"
    }

    static func nextSecondary(target: String, current: String) -> String {
        if target != current { return current }
        return outputLanguages.first { $0.token != target }?.token ?? "eng"
    }

    static func isAiSubtitle(_ filename: String?) -> Bool {
        guard let filename else { return false }
        return filename.lowercased().split(separator: ".").contains { $0 == "ai" || $0.hasPrefix("ai-") }
    }

    static func languageLabel(_ language: String?) -> String {
        let labels = [
            "chi": "中文", "chs": "简体中文", "cht": "繁体中文", "eng": "英语", "fre": "法语", "fra": "法语",
            "ger": "德语", "ita": "意大利语", "jpn": "日语", "kor": "韩语", "por": "葡萄牙语", "rus": "俄语",
            "spa": "西班牙语", "tha": "泰语",
        ]
        guard let language else { return "未知语言" }
        return labels[language] ?? language.uppercased()
    }

    static func formatLabel(_ format: String?) -> String {
        guard let format else { return "未知格式" }
        let labels = [
            "hdmv_pgs_subtitle": "PGS", "dvd_subtitle": "VobSub", "subrip": "SRT", "srt": "SRT",
            "ass": "ASS", "ssa": "SSA", "webvtt": "VTT", "vtt": "VTT",
        ]
        return labels[format.lowercased()] ?? format.uppercased()
    }

    static func candidateLabel(_ candidate: API.SourceCandidateView) -> String {
        let embedded = candidate.kind == "embedded"
        let location = embedded ? "内封" : "外挂"
        let identity = embedded ? "轨道 \((Int(candidate.key) ?? 0) + 1)" : candidate.key
        let conversion = candidate.requiresOcr ? " · 需先识别" : ""
        let provenance = [
            "original": "原始字幕", "pgs_ocr": "图片字幕识别结果", "ai": "AI 字幕", "ai_bilingual": "AI 双语成品",
        ][candidate.provenance] ?? candidate.provenance
        return "\(languageLabel(candidate.language)) · \(formatLabel(candidate.format)) · \(provenance) · \(location) \(identity)\(conversion)"
    }

    static func candidateKey(_ candidate: API.SourceCandidateView) -> String {
        "\(candidate.kind):\(candidate.key)"
    }

    static func grouped(_ value: Int) -> String {
        value.formatted(.number.locale(Locale(identifier: "zh_CN")))
    }

    static func tokenEstimate(_ tokens: Int) -> String {
        if tokens < 1000 { return "约 \(grouped(tokens)) token" }
        let k = Double(tokens) / 1000
        return tokens < 10_000 ? "约 \(String(format: "%.1f", k))k token" : "约 \(Int(k.rounded()))k token"
    }

    static func elapsed(_ seconds: Int) -> String {
        let safe = max(0, seconds)
        if safe < 60 { return "\(safe) 秒" }
        let minutes = safe / 60
        if minutes < 60 { return "\(minutes) 分 \(safe % 60) 秒" }
        return "\(minutes / 60) 小时 \(minutes % 60) 分"
    }

    static func sourceKeyLabel(_ key: String?) -> String? {
        guard let key else { return nil }
        if key.hasPrefix("embedded:") {
            if let index = Int(key.dropFirst("embedded:".count)) { return "内封轨道 \(index + 1)" }
            return "内封字幕"
        }
        return key.hasPrefix("external:") ? String(key.dropFirst("external:".count)) : key
    }

    static func runningBadgeText(_ progress: TrackGenProgress?) -> String {
        let phaseLabels = [
            "preparing": "准备中", "ocr": "识别中", "syncing": "同步检查", "glossary": "术语分析",
            "validating": "质量检查", "compressing": "质量优化", "writing": "保存中", "refreshing": "更新中",
        ]
        if progress?.phase == "translating" {
            if let percent = progress?.percentValue { return "AI \(percent)%" }
            return "翻译中"
        }
        return phaseLabels[progress?.phase ?? "preparing"] ?? "生成中"
    }
}

private extension String {
    var nonEmpty: String? { isEmpty ? nil : self }
}

// MARK: - 弹层

/// 预检 / 任务状态弹层（Web 的同一个 Modal 两种模式）
private struct TrackGenSheet: View {
    @Bindable var model: TrackGenModel
    let file: API.LibraryFileView

    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    private static let errorRed = Color(red: 1, green: 0.71, blue: 0.71)
    private static let okGreen = Color(red: 0.49, green: 0.94, blue: 0.64)

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                    if model.mode == .status {
                        statusContent
                    } else {
                        previewContent
                    }
                }
                .padding(.horizontal, Theme.pagePadding)
                .padding(.vertical, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(cancelTitle) { model.mode = nil }
                        .disabled(model.starting || model.agentStarting)
                }
            }
            .safeAreaInset(edge: .bottom) {
                actionBar
            }
        }
        .presentationDetents([.large])
        .presentationBackground(.regularMaterial)
        .interactiveDismissDisabled(model.starting || model.agentStarting)
    }

    private var title: String {
        if model.mode == .status {
            return model.running ? "正在生成\(model.activeOutputLabel)" : "AI 字幕任务"
        }
        return "生成 AI 字幕"
    }

    private var subtitle: String {
        if model.mode == .status {
            return model.running ? "后台运行，离开页面不会中断。" : "任务详情与处理建议。"
        }
        return "确认后才调用 AI，并在后台生成。"
    }

    private var cancelTitle: String {
        if model.mode == .status { return "关闭" }
        return model.blockedWithoutPgs || model.requestError != nil ? "关闭" : "取消"
    }

    // MARK: 状态模式

    @ViewBuilder
    private var statusContent: some View {
        if model.running, let progress = model.progress {
            TrackGenProgressView(progress: progress)
                .padding(16)
                .background(Theme.info.opacity(0.07), in: .rect(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Theme.info.opacity(0.25)))
            errorBox
        } else {
            let succeeded = model.jobSucceeded
            let color = succeeded ? Self.okGreen : Self.errorRed
            VStack(alignment: .leading, spacing: 6) {
                // 结束态也报一句目标输出：Agent、CLI 发起的任务用户没在这里选过语言
                Text("\(model.activeOutputLabel)\(succeeded ? "字幕生成完成" : "字幕生成未完成")")
                    .font(.callout.weight(.semibold))
                Text(resultMessage)
                    .font(.subheadline)
                    .opacity(0.85)
            }
            .foregroundStyle(color)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background((succeeded ? Theme.success : Theme.danger).opacity(0.08), in: .rect(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder((succeeded ? Theme.success : Theme.danger).opacity(0.3)))
            errorBox
        }
    }

    private var resultMessage: String {
        guard let job = model.job else { return "任务已经结束。" }
        if let message = job.error?["message"]?.stringValue { return message }
        if case let .string(message)? = job.result?["message"] { return message }
        return "任务已经结束。"
    }

    // MARK: 预检模式

    @ViewBuilder
    private var previewContent: some View {
        outputLanguageCard

        if model.previewing {
            HStack(alignment: .top, spacing: 12) {
                ProgressView()
                VStack(alignment: .leading, spacing: 4) {
                    Text(model.pendingNotice ?? "正在检查参考字幕，不会调用 AI…")
                        .font(.callout.weight(.medium))
                        .foregroundStyle(Theme.text)
                    // 内封字幕要把整个视频通读一遍，大文件是分钟级；说清楚为什么慢
                    if model.pendingNotice != nil {
                        Text("首次读取内封字幕需要通读整个视频文件，读好后会自动继续；这一步不会调用 AI，也不产生费用。")
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .modifier(TrackGenCard())
        } else {
            if let preview = model.preview, !preview.candidates.isEmpty {
                sourceCard(preview)
            }
            if model.requestError != nil { errorBox }
            if model.canPreparePgs, let conversion = model.preview?.pgsConversion {
                pgsCard(conversion)
            }
            if model.blockedWithoutPgs, let blocker = model.preview?.blocker {
                blockerCard(blocker)
            }
            if let preview = model.preview, preview.blocker == nil, let chosen = model.chosen {
                chosenCard(preview, chosen: chosen)
            }
        }
    }

    private var outputLanguageCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("输出语言")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Color.white.opacity(0.85))
            languageMenu(
                caption: model.bilingual ? "第一行语言" : "目标语言",
                selection: model.targetLanguage,
                options: TrackGenText.outputLanguages
            ) { model.changeTarget($0, api: api, fileId: file.id) }
            if model.bilingual {
                languageMenu(
                    caption: "第二行语言",
                    selection: model.secondaryLanguage,
                    options: TrackGenText.outputLanguages.filter { $0.token != model.targetLanguage }
                ) { model.changeSecondary($0, api: api, fileId: file.id) }
            }
            Toggle("生成双语字幕", isOn: Binding(
                get: { model.bilingual },
                set: { model.changeBilingual($0, api: api, fileId: file.id) }
            ))
            .font(.subheadline)
            .tint(Theme.info)
            .disabled(model.starting)
            if model.bilingual {
                Text("每条字幕固定两行，上下顺序按这里的选择生成。")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
            }
        }
        .padding(14)
        .modifier(TrackGenCard())
    }

    private func languageMenu(
        caption: String, selection: String, options: [(token: String, label: String)],
        onSelect: @escaping (String) -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(caption)
                .font(.caption)
                .foregroundStyle(Theme.textMuted)
            Menu {
                ForEach(options, id: \.token) { option in
                    Button {
                        onSelect(option.token)
                    } label: {
                        if option.token == selection {
                            Label(option.label, systemImage: "checkmark")
                        } else {
                            Text(option.label)
                        }
                    }
                }
            } label: {
                TrackGenMenuLabel(text: TrackGenText.outputLanguageLabel(selection))
            }
            .disabled(model.starting)
        }
    }

    private func sourceCard(_ preview: API.GenPreviewView) -> some View {
        let anySelectable = preview.candidates.contains { $0.selectable }
        let current = preview.candidates.first { TrackGenText.candidateKey($0) == model.sourceCandidateKey }
        return VStack(alignment: .leading, spacing: 8) {
            Text("参考字幕")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Color.white.opacity(0.85))
            Menu {
                ForEach(preview.candidates, id: \.self) { candidate in
                    let key = TrackGenText.candidateKey(candidate)
                    Button {
                        model.loadPreview(api: api, fileId: file.id, sourceKey: key)
                    } label: {
                        let text = TrackGenText.candidateLabel(candidate)
                            + (candidate.selectable ? "" : "（不可用：\(candidate.excluded ?? "不支持")）")
                        if key == model.sourceCandidateKey {
                            Label(text, systemImage: "checkmark")
                        } else {
                            Text(text)
                        }
                    }
                    .disabled(!candidate.selectable)
                }
            } label: {
                TrackGenMenuLabel(text: current.map(TrackGenText.candidateLabel) ?? "没有可用的参考字幕")
            }
            .disabled(model.starting || !anySelectable)
            Text("默认优先英语。也可以指定其他内封或外挂字幕；选择 PGS 时会先识别文字，再开始 AI 翻译。")
                .font(.caption)
                .foregroundStyle(Theme.textFaint)
            if model.selectedCandidate?.requiresOcr == true {
                Text("当前选择的是图片字幕，需要先完成文字识别。")
                    .font(.caption)
                    .foregroundStyle(Theme.warning.opacity(0.85))
            }
        }
        .padding(14)
        .modifier(TrackGenCard())
    }

    private func pgsCard(_ conversion: API.PgsConversionView) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(conversion.languageConfirmationRequired ? "请选择原字幕语言" : "先识别图片字幕")
                    .font(.callout.weight(.semibold))
                Text("这份字幕是图片。MovieClaw 会先识别其中的文字，确认内容完整后再生成\(model.currentOutputLabel)字幕。")
                    .font(.subheadline)
                    .opacity(0.85)
            }
            .foregroundStyle(Theme.warning)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(Theme.warning.opacity(0.08), in: .rect(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Theme.warning.opacity(0.3)))

            VStack(alignment: .leading, spacing: 8) {
                if conversion.languageConfirmationRequired {
                    Text("原字幕语言")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Color.white.opacity(0.85))
                    Menu {
                        ForEach(conversion.languageOptions, id: \.code) { option in
                            Button {
                                model.pgsOcrLanguage = option.code
                            } label: {
                                if option.code == model.pgsOcrLanguage {
                                    Label(option.label, systemImage: "checkmark")
                                } else {
                                    Text(option.label)
                                }
                            }
                        }
                    } label: {
                        TrackGenMenuLabel(text: conversion.languageOptions.first { $0.code == model.pgsOcrLanguage }?.label ?? "请选择字幕语言")
                    }
                    Text("请选择画面中实际显示的语言。")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                } else {
                    Text("原字幕语言：\(conversion.ocrLanguageLabel ?? "已自动识别")")
                        .foregroundStyle(Color.white.opacity(0.75))
                }
                Divider().overlay(Color.white.opacity(0.07))
                Text("原影片和字幕不会被修改。识别结果可能有少量错字，完成后建议抽查人名与特殊字体。")
            }
            .font(.subheadline)
            .foregroundStyle(Theme.textMuted)
            .padding(14)
            .modifier(TrackGenCard())
        }
    }

    private func blockerCard(_ blocker: API.GenPreviewBlockerView) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(blocker.title).font(.callout.weight(.semibold))
                Text(blocker.message).font(.subheadline).opacity(0.85)
            }
            .foregroundStyle(Self.errorRed)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(Theme.danger.opacity(0.08), in: .rect(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Theme.danger.opacity(0.3)))

            if !blocker.suggestions.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("可以这样处理")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Color.white.opacity(0.85))
                    ForEach(blocker.suggestions, id: \.self) { suggestion in
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text("•")
                            Text(suggestion)
                        }
                        .font(.subheadline)
                        .foregroundStyle(Color.white.opacity(0.75))
                    }
                }
            }
            if let conversion = model.preview?.pgsConversion {
                DisclosureGroup("查看诊断信息") {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("运行环境：\(conversion.platform) \(conversion.architecture)" + (conversion.engine.map { " · \($0)" } ?? " · 未找到可用识别引擎"))
                        if conversion.message != blocker.message {
                            Text(conversion.message)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 6)
                }
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .tint(Color.white.opacity(0.7))
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .modifier(TrackGenCard())
            }
        }
    }

    private func chosenCard(_ preview: API.GenPreviewView, chosen: API.SourceCandidateView) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                Text("\(Text(TrackGenText.candidateLabel(chosen)).foregroundStyle(Theme.text))\(Text("  →  ").foregroundStyle(Theme.textFaint))\(Text(model.currentOutputLabel).foregroundStyle(Theme.info))")
                    .font(.callout.weight(.semibold))
                Text("\(TrackGenText.grouped(preview.eventCount)) 条对白 · \(TrackGenText.tokenEstimate(preview.estimatedTokens))")
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .modifier(TrackGenCard())

            Text("生成同目录 \(Text(preview.outputFilename ?? "规范命名的 AI 字幕文件").foregroundStyle(Color.white.opacity(0.8)))，原字幕不变；离开页面不影响生成。")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)

            if preview.alreadyGenerated {
                Text("已有 AI 字幕，将被覆盖。")
                    .font(.subheadline)
                    .foregroundStyle(Theme.warning.opacity(0.9))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(Theme.warning.opacity(0.07), in: .rect(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.warning.opacity(0.25)))
            }
        }
    }

    @ViewBuilder
    private var errorBox: some View {
        if let error = model.requestError {
            Text(error)
                .font(.subheadline)
                .foregroundStyle(Self.errorRed)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Theme.danger.opacity(0.08), in: .rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.danger.opacity(0.3)))
        }
    }

    // MARK: 底部动作条（「取消 / 关闭」在导航栏左上角）

    @ViewBuilder
    private var actionBar: some View {
        let buttons = actionButtons
        if !buttons.isEmpty {
            HStack(spacing: 10) {
                Spacer(minLength: 0)
                ForEach(buttons) { $0.view }
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.vertical, 10)
        }
    }

    private struct ActionButton: Identifiable {
        let id: String
        let view: AnyView
    }

    private var actionButtons: [ActionButton] {
        var buttons: [ActionButton] = []
        if model.mode == .status {
            if model.running {
                buttons.append(ActionButton(id: "stop", view: AnyView(
                    Button(model.stopping ? "正在请求停止…" : "停止生成", role: .destructive) {
                        Task { await model.stop(api: api) }
                    }
                    .buttonStyle(.glass)
                    .disabled(model.stopping)
                )))
            } else if !model.jobSucceeded {
                buttons.append(ActionButton(id: "recheck", view: AnyView(
                    Button("重新预检") { model.loadPreview(api: api, fileId: file.id) }
                        .buttonStyle(.glassProminent)
                )))
                buttons.append(agentButton(reason: resultMessage))
            }
            return buttons
        }
        guard !model.previewing else { return [] }
        let blocked = model.blockedWithoutPgs
        if blocked || (model.preview == nil && model.requestError != nil) {
            buttons.append(ActionButton(id: "recheck", view: AnyView(
                Button("重新检查") { model.loadPreview(api: api, fileId: file.id) }
                    .buttonStyle(.glass)
            )))
        }
        if blocked || model.requestError != nil {
            buttons.append(agentButton(
                reason: model.requestError ?? model.preview?.blocker?.message ?? "字幕生成预检没有通过"
            ))
        }
        if model.canPreparePgs {
            buttons.append(ActionButton(id: "start-pgs", view: AnyView(
                Button(model.starting ? "正在启动…" : "开始生成\(model.currentOutputLabel)") {
                    Task { await model.start(api: api, fileId: file.id) }
                }
                .buttonStyle(.glassProminent)
                .disabled(model.starting || !model.canConvertPgs)
            )))
        }
        if let preview = model.preview, preview.blocker == nil, model.chosen != nil {
            buttons.append(ActionButton(id: "start", view: AnyView(
                Button(model.starting ? "正在启动…" : "确认生成\(model.currentOutputLabel)") {
                    Task { await model.start(api: api, fileId: file.id) }
                }
                .buttonStyle(.glassProminent)
                .disabled(model.starting)
            )))
        }
        return buttons
    }

    private func agentButton(reason: String) -> ActionButton {
        ActionButton(id: "agent", view: AnyView(
            Button(model.agentStarting ? "正在交给 Agent…" : "交给 Agent 处理") {
                Task {
                    if let sessionId = await model.handOffToAgent(api: api, file: file, reason: reason) {
                        router.open(.session(id: sessionId))
                    }
                }
            }
            .buttonStyle(.glass)
            .tint(Theme.info)
            .disabled(model.agentStarting)
        ))
    }
}

/// 运行中的进度详情：消息 + 百分比条 + 计数 + 五个阶段（同 Web ProgressDetails）
private struct TrackGenProgressView: View {
    let progress: TrackGenProgress

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top) {
                Text(progress.message.isEmpty ? "正在准备生成任务" : progress.message)
                    .font(.callout.weight(.semibold))
                Spacer(minLength: 8)
                if let percent = progress.percentValue {
                    Text("\(percent)%").font(.callout.weight(.semibold).monospacedDigit())
                }
            }
            .foregroundStyle(Theme.info)

            if let percent = progress.percentValue {
                ProgressView(value: Double(percent), total: 100).tint(Theme.info)
            } else {
                ProgressView().progressViewStyle(.linear).tint(Theme.info)
            }

            TrackFlowLayout(spacing: 14, lineSpacing: 4) {
                if progress.totalBlocks > 0 { Text("翻译块 \(progress.doneBlocks)/\(progress.totalBlocks)") }
                if progress.totalEvents > 0 { Text("对白 \(progress.doneEvents)/\(progress.totalEvents) 条") }
                if let reference = TrackGenText.sourceKeyLabel(progress.sourceCandidateKey) {
                    Text("参考 \(reference)").lineLimit(1).truncationMode(.middle)
                }
                if progress.modelRequests > 0 {
                    Text("模型调用 \(progress.modelRequests) 次" + (progress.modelTokens > 0 ? " · \(TrackGenText.grouped(progress.modelTokens)) token" : ""))
                }
                Text("已用时 \(TrackGenText.elapsed(progress.elapsedSeconds))")
            }
            .font(.caption.monospacedDigit())
            .foregroundStyle(Theme.textMuted)

            if !progress.activeBlocks.isEmpty {
                Text((progress.parallelism > 1 ? "并发上限 \(progress.parallelism) 路 · " : "")
                    + "正在处理第 \(progress.activeBlocks.map(String.init).joined(separator: "、")) 块"
                    + (progress.oldestActiveSeconds > 0 ? " · 最早一块已等待 \(TrackGenText.elapsed(progress.oldestActiveSeconds))" : ""))
                    .font(.caption)
                    .foregroundStyle(Theme.info.opacity(0.85))
            }
            if progress.rateLimitCount > 0 || progress.validationRetries > 0 {
                Text((progress.rateLimitCount > 0 ? "模型服务繁忙 \(progress.rateLimitCount) 次，系统已自动降速重试。" : "")
                    + (progress.validationRetries > 0 ? "有 \(progress.validationRetries) 次返回格式不完整，系统已自动纠正。" : ""))
                    .font(.caption)
                    .foregroundStyle(Color(red: 1, green: 0.85, blue: 0.6))
            }

            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(TrackGenText.stages.enumerated()), id: \.offset) { index, stage in
                    let completed = index < progress.stageIndex
                    let current = index == progress.stageIndex
                    HStack(spacing: 8) {
                        ZStack {
                            Circle()
                                .fill(current ? Theme.info.opacity(0.15) : completed ? Theme.success.opacity(0.1) : .clear)
                            Circle()
                                .strokeBorder(current ? Theme.info : completed ? Theme.success.opacity(0.6) : Color.white.opacity(0.15))
                            Text(completed ? "✓" : "\(index + 1)")
                                .font(.system(size: 9))
                                .foregroundStyle(completed ? Color(red: 0.49, green: 0.94, blue: 0.64) : Color.primary)
                        }
                        .frame(width: 16, height: 16)
                        Text(index == 0 && progress.usesOcr ? "识别并检查图片字幕" : stage.label)
                        if current {
                            Spacer(minLength: 8)
                            Text("进行中").foregroundStyle(Theme.textMuted)
                        }
                    }
                    .font(current ? .caption.weight(.medium) : .caption)
                    .foregroundStyle(current ? Theme.info : completed ? Color.white.opacity(0.7) : Color.white.opacity(0.35))
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("字幕生成阶段")
        }
    }
}

/// 下拉菜单的触发外观：当前值 + 上下箭头，占满一行
private struct TrackGenMenuLabel: View {
    let text: String

    var body: some View {
        HStack {
            Text(text)
                .lineLimit(2)
                .multilineTextAlignment(.leading)
            Spacer(minLength: 8)
            Image(systemName: "chevron.up.chevron.down").font(.caption)
        }
        .font(.callout)
        .foregroundStyle(Theme.text)
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .frame(maxWidth: .infinity)
        .background(Color.white.opacity(0.06), in: .rect(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.1)))
    }
}

/// 弹层里的普通信息卡底
private struct TrackGenCard: ViewModifier {
    func body(content: Content) -> some View {
        content
            .background(Color.white.opacity(0.035), in: .rect(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.white.opacity(0.08)))
    }
}

nonisolated extension APIClient {
    /// 生成预检（`GET /libraries/files/{id}/subtitles/generation-preview`）。
    ///
    /// 与生成函数同参，但带 20 秒超时（同 Web `PREVIEW_TIMEOUT_MS`）：后端保证不在请求里
    /// 等 ffmpeg 通读大文件（没抽好就回 pending），超过 20 秒就是真的不对劲。
    fileprivate func trackRowsGenerationPreview(
        fileId: Int, targetLanguage: String, secondaryLanguage: String?, sourceCandidateKey: String?
    ) async throws -> API.GenPreviewView {
        var query = [URLQueryItem(name: "target_language", value: targetLanguage)]
        if let secondaryLanguage { query.append(URLQueryItem(name: "secondary_language", value: secondaryLanguage)) }
        if let sourceCandidateKey { query.append(URLQueryItem(name: "source_candidate_key", value: sourceCandidateKey)) }
        return try await send(
            "GET", "/libraries/files/\(fileId)/subtitles/generation-preview", query: query, timeout: 20
        )
    }
}
