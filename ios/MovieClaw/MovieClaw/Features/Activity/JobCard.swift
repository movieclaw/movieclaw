import SwiftUI

/// 需要处理区的后台作业卡（Web `JobCard`）。
///
/// 结构：状态点 · 类型标签 · 标题 · ⋯ 菜单 / 摘要（需要处理时红框 +「需要处理：」前缀）/ 本次新增文件 /
/// 阶段进度 / 领域明细标签 / 模型消耗（字幕任务）/ 发起方 · 时间 · 已忽略。
///
/// 菜单顺序刻意是「先出路、后出口」：取消 → 错误里带的补救动作（重试 / 去设置 / 看日志 / 交给 AI）→
/// 忽略这个任务（失败且未忽略时）→ 撤销忽略。忽略只开给失败任务：活跃任务（含 blocked）要终结得走取消——
/// 它们仍占着去重键与资源锁，光藏掉提醒会留下一个看不见却挡着同名任务再次创建的幽灵。
struct JobCard: View {
    let job: API.JobView
    let store: TaskActivityStore

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @Environment(Feedback.self) private var feedback
    @State private var busyAction: String?
    @State private var actionError: String?
    @State private var usageExpanded = false
    @State private var dismissOpen = false
    @State private var filesOpen = false

    private var probe: LLMCapabilityProbe { .shared }

    var body: some View {
        let active = TaskCenter.activeFeedJobStatuses.contains(job.status)
        let dismissed = TaskCenter.isDismissed(job)
        let attention = (job.status == "blocked" || job.status == "failed") && !dismissed
        let details = TaskCenter.jobDetailItems(job)
        let hasDomainAlert = details.contains(where: \.alert)
        let compact = (job.status == "succeeded" || job.status == "cancelled") && !hasDomainAlert
        let meta = TaskCenter.jobStatusMeta(job.status)
        let title = TaskCenter.nonEmpty(job.subject) ?? TaskCenter.jobTypeLabel(job)
        let completedLanguage = job.status == "succeeded" ? TaskCenter.subtitleLanguageLabel(job) : nil
        let summary = completedLanguage.map { "已生成\($0)字幕" } ?? TaskCenter.errorMessage(job) ?? job.progress.message
        let borderColor = attention ? Theme.danger.opacity(0.2) : hasDomainAlert ? Theme.warning.opacity(0.15) : Color.white.opacity(0.08)

        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 9) {
                ActivityStatusDot(color: meta.color, pulse: meta.pulse, size: 10, label: meta.label).padding(.top, 5)
                if job.subject != nil {
                    Text(TaskCenter.jobTypeLabel(job))
                        .font(.caption2.weight(.medium)).foregroundStyle(Theme.textMuted)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Color.white.opacity(0.045), in: .rect(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Theme.line))
                        .fixedSize()
                }
                Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.9)).lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                actionsMenu(active: active, dismissed: dismissed).padding(.top, -4)
            }

            Group {
                if attention {
                    Text.activityJoin([Text("需要处理：").fontWeight(.semibold), Text(summary)])
                } else {
                    Text(summary)
                }
            }
            .font(compact ? .caption : .footnote)
            .foregroundStyle(attention ? Theme.danger : compact ? Theme.textFaint : Theme.textMuted)
            .lineLimit(compact ? 1 : 3)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(attention ? 11 : 0)
            .background(attention ? Theme.danger.opacity(0.06) : .clear, in: .rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(attention ? Theme.danger.opacity(0.15) : .clear))
            .padding(.top, compact ? 4 : 10)

            let imported = job.jobType == "library.ingest" ? TaskCenter.strings(job.progress.details, "imported_files") : []
            if !imported.isEmpty { importedFiles(imported) }

            if !compact, !(job.jobType == "library.scan" && job.status == "succeeded"),
               job.progress.percent != nil || job.status == "running" || job.status == "cancelling" || job.status == "retry_wait" {
                progressBlock
            }

            if !compact, !details.isEmpty {
                DiscoverFlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(details, id: \.self) { item in
                        Text(item.label)
                            .font(.caption).lineLimit(1)
                            .foregroundStyle(item.alert ? Color(red: 1, green: 0.95, blue: 0.8).opacity(0.75) : Theme.textFaint)
                            .padding(.horizontal, 8).padding(.vertical, 4)
                            .background(item.alert ? Theme.warning.opacity(0.05) : Color.white.opacity(0.035), in: .rect(cornerRadius: 6))
                            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(item.alert ? Theme.warning.opacity(0.15) : Color.white.opacity(0.07)))
                    }
                }
                .padding(.top, 12)
            }

            if let usage = TaskCenter.subtitleUsageSummary(job) { usageBlock(usage) }

            if let actionError {
                Text(actionError).font(.caption).foregroundStyle(Color(red: 1, green: 0.62, blue: 0.62)).padding(.top, 8)
            }

            HStack(spacing: 8) {
                Text("\(TaskCenter.cardOriginLabel(job)) · \(ActivityFormat.relative(job.createdAt))")
                    .font(.caption2).foregroundStyle(Theme.textFaint.opacity(0.7)).lineLimit(1)
                    .accessibilityHint(ActivityFormat.dateTime(job.createdAt))
                Spacer()
                if dismissed {
                    Text("已忽略").font(.caption2).foregroundStyle(Theme.textFaint)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Theme.line))
                }
            }
            .padding(.top, compact ? 6 : 12)
        }
        .padding(compact ? 12 : 14)
        .background(Color(red: 14 / 255, green: 16 / 255, blue: 22 / 255).opacity(0.52), in: .rect(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(borderColor))
        .task { await probe.ensure(api: api) }
        .sheet(isPresented: $dismissOpen) {
            DismissJobSheet(job: job) { mute in await dismiss(muteSource: mute) }
                .sheetFeedback()
        }
        .accessibilityIdentifier("job-card-\(job.id)")
    }

    // MARK: 菜单

    private func actionsMenu(active: Bool, dismissed: Bool) -> some View {
        let cancellable = active && job.status != "cancelling"
        let dismissable = job.status == "failed" && !dismissed
        var actions = TaskCenter.errorActions(job).filter {
            TaskCenter.supportedErrorActions.contains($0.type) && ($0.type != "handoff_agent" || probe.allowsHandoff)
        }
        if job.status == "cancelled", !TaskCenter.isSystemCancelled(job), !actions.contains(where: { $0.type == "retry_job" }) {
            actions.insert(TaskCenter.ErrorAction(type: "retry_job", label: "重新执行"), at: 0)
        }
        let hasItems = cancellable || !actions.isEmpty || dismissable || dismissed
        return Group {
            if hasItems {
                Menu {
                    if cancellable {
                        Button(busyAction == "cancel" ? "正在取消…" : "取消任务", systemImage: "stop.circle") { cancel() }
                    }
                    ForEach(actions, id: \.self) { action in
                        Button(busyAction == action.type ? "处理中…" : action.label) { run(action) }
                    }
                    if dismissable {
                        Button("忽略这个任务", systemImage: "eye.slash") { dismissOpen = true }
                    }
                    if dismissed {
                        Button(busyAction == "undismiss" ? "撤销中…" : "撤销忽略", systemImage: "arrow.uturn.backward") { undismiss() }
                    }
                } label: {
                    Image(systemName: "ellipsis")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Theme.textMuted)
                        .frame(width: 30, height: 28)
                        .contentShape(.rect)
                }
                .disabled(busyAction != nil)
                .accessibilityLabel("\(TaskCenter.nonEmpty(job.subject) ?? TaskCenter.jobTypeLabel(job))的更多操作")
                .accessibilityIdentifier("job-actions-\(job.id)")
            }
        }
    }

    // MARK: 区块

    private var progressBlock: some View {
        let percent = job.progress.percent
        let waitingProgress = job.progress.mode == "waiting"
        let phase = TaskCenter.jobPhaseLabels[job.progress.phase] ?? job.progress.phase
        let step: String? = if let index = job.progress.phaseIndex, let count = job.progress.phaseCount { "\(index)/\(count)" } else { nil }
        let accent: Color = switch job.status {
        case "succeeded": Theme.success
        case "failed", "blocked": Theme.danger
        case "retry_wait", "cancelling": Theme.warning
        default: Theme.info
        }
        let amount = TaskCenter.cardProgressAmount(job)
        return VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text.activityJoin([Text(phase).foregroundStyle(Theme.textMuted), Text(step.map { "  阶段 \($0)" } ?? "").foregroundStyle(Theme.textFaint)])
                    .fontWeight(.medium).lineLimit(1)
                Spacer()
                Text(percent.map { "\(Int($0.rounded()))%" }
                    ?? (job.status == "retry_wait" ? "等待继续" : waitingProgress ? "等待中" : "处理中"))
                    .monospacedDigit().foregroundStyle(Theme.textFaint)
            }
            .font(.caption)
            ActivityProgressBar(
                percent: percent, color: accent,
                indeterminateOpacity: job.status == "retry_wait" || waitingProgress ? 0.35 : 0.7
            )
            if amount != nil || job.status == "retry_wait" {
                Text([amount, job.status == "retry_wait" ? "第 \(max(1, job.attempt)) / \(job.maxAttempts) 次尝试" : nil]
                    .compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).monospacedDigit().foregroundStyle(Theme.textFaint)
            }
        }
        .padding(.top, 12)
    }

    /// 本次作业实际新增的文件：单个直接展示，多个保留首项并可展开核对
    private func importedFiles(_ files: [String]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                if files.count > 1 { withAnimation(.snappy(duration: 0.2)) { filesOpen.toggle() } }
            } label: {
                HStack(spacing: 6) {
                    if files.count > 1 {
                        Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).rotationEffect(.degrees(filesOpen ? 90 : 0))
                    }
                    Text(files.count == 1 ? "本次新增文件" : "本次新增 \(files.count) 个文件").foregroundStyle(Theme.textFaint).fixedSize()
                    Text(files[0]).fontWeight(.medium).foregroundStyle(Theme.textMuted).lineLimit(1)
                }
                .font(.caption)
                .padding(.horizontal, 10).padding(.vertical, 7)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            if filesOpen {
                Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1)
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(files, id: \.self) { Text($0).textSelection(.enabled) }
                }
                .font(.caption).foregroundStyle(Theme.textMuted)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.white.opacity(0.07)))
        .padding(.top, 10)
    }

    /// 字幕任务的 LLM Token 用量
    private func usageBlock(_ usage: TaskCenter.UsageSummary) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1)
            Button {
                withAnimation(.snappy(duration: 0.2)) { usageExpanded.toggle() }
            } label: {
                HStack {
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).rotationEffect(.degrees(usageExpanded ? 90 : 0))
                    Text(usageExpanded ? "收起模型消耗" : "查看模型消耗").fontWeight(.medium)
                    Spacer()
                    Text("\(ActivityFormat.integer(usage.totalTokens)) Token · \(usage.requests) 次请求")
                        .monospacedDigit().foregroundStyle(Theme.textFaint).lineLimit(1)
                }
                .font(.caption)
                .foregroundStyle(Theme.textMuted)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("usage-toggle-\(job.id)")
            if usageExpanded {
                VStack(alignment: .leading, spacing: 10) {
                    Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 10) {
                        GridRow {
                            usageMetric("总 Token", usage.totalTokens)
                            usageMetric("输入", usage.promptTokens)
                        }
                        GridRow {
                            usageMetric("输出", usage.completionTokens)
                            usageMetric("输入缓存", usage.cacheReadTokens)
                        }
                    }
                    Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1)
                    DiscoverFlowLayout(spacing: 12, lineSpacing: 4) {
                        if let provider = job.providerRef { Text(provider) }
                        Text("\(usage.requests) 次模型请求")
                        if usage.failedRequests > 0 {
                            Text("\(usage.failedRequests) 次失败").foregroundStyle(Theme.warning.opacity(0.7))
                        }
                        Text("累计响应 \(TaskCenter.formatModelDuration(usage.totalDurationMs))")
                        Text("最慢一次 \(TaskCenter.formatModelDuration(usage.maxDurationMs))")
                    }
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                }
                .padding(12)
                .background(Color.black.opacity(0.15), in: .rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.07)))
            }
        }
        .padding(.top, 12)
    }

    private func usageMetric(_ label: String, _ value: Int) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label).font(.caption2).foregroundStyle(Theme.textFaint)
            Text(ActivityFormat.integer(value)).font(.subheadline.weight(.semibold)).monospacedDigit().foregroundStyle(Theme.text.opacity(0.7))
        }
    }

    // MARK: 动作

    private func perform(_ key: String, fallback: String, _ work: @escaping () async throws -> Void) {
        busyAction = key
        actionError = nil
        Task {
            defer { busyAction = nil }
            do {
                try await work()
            } catch {
                actionError = error.localizedDescription.isEmpty ? fallback : error.localizedDescription
            }
        }
    }

    private func cancel() {
        perform("cancel", fallback: "取消任务失败，请稍后重试") {
            store.upsert(try await api.jobsCancel(jobId: job.id).job)
        }
    }

    private func undismiss() {
        perform("undismiss", fallback: "撤销忽略失败，请稍后重试") {
            store.upsert(try await api.jobsUndismiss(jobId: job.id).job)
        }
    }

    private func dismiss(muteSource: Bool) async {
        busyAction = "dismiss"
        actionError = nil
        defer { busyAction = nil }
        do {
            store.upsert(try await api.jobsDismiss(jobId: job.id, body: API.JobDismissRequest(muteSource: muteSource)).job)
            dismissOpen = false
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "忽略失败，请稍后重试" : error.localizedDescription)
        }
    }

    private func run(_ action: TaskCenter.ErrorAction) {
        perform(action.type, fallback: "操作未完成，请稍后重试") {
            switch action.type {
            case "retry_job":
                store.upsert(try await api.jobsRetry(jobId: job.id).job)
            case "handoff_agent":
                // 工单由后端组装（带事件时间线、现场自检、与界面一致的动作清单）
                let sessionId = try await api.activityHandoff(kind: "job", ref: job.id)
                router.open(.session(id: sessionId))
            default:
                let section: SettingsSection = switch action.type {
                case "inspect_logs": .logs
                case "update_runtime": .app
                default: action.target.flatMap(SettingsSection.init(rawValue:)) ?? .app
                }
                router.open(.settingsSection(section))
            }
        }
    }
}

/// 忽略的二次确认：真正要问的是**忽略到哪一层**——只忽略这一条，还是连自动来源一起静音。
/// 有自动来源的任务类型（字幕自动生成）默认勾上：用户说了不处理，多半也不想下次扫描再被叫醒一次。
struct DismissJobSheet: View {
    let job: API.JobView
    let onConfirm: (Bool) async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var muteSource: Bool
    @State private var busy = false

    init(job: API.JobView, onConfirm: @escaping (Bool) async -> Void) {
        self.job = job
        self.onConfirm = onConfirm
        _muteSource = State(initialValue: TaskCenter.autoRecreatedJobTypes[job.jobType] != nil)
    }

    var body: some View {
        let muteLabel = TaskCenter.autoRecreatedJobTypes[job.jobType]
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text("它会从「需要处理」下架，移到「已结束」。任务记录、失败原因和重试入口都还在，随时可以撤销忽略。")
                        .font(.subheadline).foregroundStyle(Theme.textMuted)
                    Text(TaskCenter.nonEmpty(job.subject) ?? TaskCenter.jobTypeLabel(job))
                        .font(.subheadline).foregroundStyle(Theme.text.opacity(0.75))
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.white.opacity(0.035), in: .rect(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
                    if let muteLabel {
                        Toggle(isOn: $muteSource) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(muteLabel).font(.subheadline.weight(.semibold))
                                Text("不勾选的话，下次扫描媒体库时系统还会自动重新生成一次、再失败一次。手动生成不受影响，你随时可以自己再试。")
                                    .font(.caption).foregroundStyle(Theme.textMuted)
                            }
                        }
                        .padding(12)
                        .background(muteSource ? Color.white.opacity(0.07) : Color.white.opacity(0.025), in: .rect(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(muteSource ? 0.16 : 0.08)))
                        .accessibilityIdentifier("dismiss-mute-toggle")
                    }
                    Button {
                        busy = true
                        Task {
                            await onConfirm(muteSource)
                            busy = false
                        }
                    } label: {
                        Text(busy ? "正在忽略…" : "忽略")
                            .font(.body.weight(.semibold))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 4)
                    }
                    .buttonStyle(.glassProminent)
                    .disabled(busy)
                    .padding(.top, 6)
                    .accessibilityIdentifier("dismiss-job-confirm")
                }
                .padding(20)
            }
            .navigationTitle("忽略这个任务？")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }.disabled(busy)
                }
            }
        }
        .presentationDetents([.medium, .large])
        .interactiveDismissDisabled(busy)
    }
}
