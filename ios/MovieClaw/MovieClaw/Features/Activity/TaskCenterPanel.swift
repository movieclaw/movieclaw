import SwiftUI

/// 任务视角（Web `TaskCenterView`）：统一「观察入口」，不制造统一状态表。
///
/// Job 状态来自 MovieClaw 数据库（SSE 实时推送），下载状态来自下载器实时快照（10 秒轮询），
/// 订阅关系仅按 infohash 投影；各自的取消、重试和入库生命周期仍由原领域负责。
/// 页面按「是否需要用户行动」组织：需要你处理（置顶红框）→ 现在（时间线）→ 刷流做种（折叠）→ 已结束（按天）。
struct TaskCenterPanel: View {
    let store: TaskActivityStore
    @Binding var view: TaskSlice

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(Router.self) private var router

    @State private var replacingTaskId: String?
    @State private var cancellingJobId: String?
    @State private var retryingJobId: String?
    @State private var undismissingJobId: String?
    @State private var bulkDismissing = false
    @State private var pendingDelete: API.DownloadTaskView?

    var body: some View {
        let activity = store.activity
        let showAttention = view == .all || view == .attention
        let showActive = view == .all || view == .active
        let showHistory = view == .all || view == .history
        let visibleCount = (showAttention ? activity.attentionTotal : 0)
            + (showActive ? activity.activeTotal : 0)
            + (showActive && !activity.boostTasks.isEmpty ? 1 : 0)
            + (showHistory ? activity.historyTotal : 0)
        let hasContentBeforeHistory = (showAttention && activity.attentionTotal > 0) || (showActive && activity.activeTotal > 0)

        VStack(alignment: .leading, spacing: 0) {
            let failedSources = store.sources.filter { $0.status != "active" }
            if !failedSources.isEmpty || store.downloadsError != nil {
                sourceWarning(failedSources)
            }
            tabs(activity)

            if showAttention, activity.attentionTotal > 0 {
                attentionSection(activity)
            }
            if showActive, activity.activeTotal > 0 {
                activeSection(activity)
            }
            if showActive, !activity.boostTasks.isEmpty {
                BoostTaskSection(tasks: activity.boostTasks).padding(.top, 12)
            }
            if showHistory, !activity.standaloneHistoricalJobs.isEmpty {
                TaskHistorySection(
                    jobs: activity.standaloneHistoricalJobs, initiallyOpen: view == .history,
                    separated: hasContentBeforeHistory,
                    retryingJobId: retryingJobId, undismissingJobId: undismissingJobId,
                    onRetry: retry, onUndismiss: undismiss
                )
            }
            if visibleCount == 0 {
                if store.downloadsLoading || !store.jobsLoaded {
                    HStack(spacing: 10) {
                        ProgressView()
                        Text("正在汇总任务…").font(.subheadline).foregroundStyle(Theme.textMuted)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 70)
                } else {
                    emptyView
                }
            }
        }
        .sheet(item: $pendingDelete) { task in
            DeleteDownloadTaskSheet(task: task) { deleteFiles in
                await delete(task, deleteFiles: deleteFiles)
            }
            .sheetFeedback()
        }
    }

    // MARK: 头部

    private func sourceWarning(_ sources: [API.DownloadTaskSourceView]) -> some View {
        let message = store.downloadsError
            ?? sources.map { "「\($0.name)」\($0.message.flatMap { $0.isEmpty ? nil : $0 } ?? "当前不可用")" }.joined(separator: "；")
        return ActivityWarningBanner(message: message) {
            Button("检查设置") { router.open(.settingsSection(.downloaders)) }
                .font(.subheadline.weight(.semibold))
                .buttonStyle(.plain)
        }
        .padding(.top, 16)
    }

    private func tabs(_ activity: TaskCenter.Activity) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ActivitySliceTabs(
                slices: TaskSlice.allCases, selection: view, label: \.label,
                count: { slice in
                    switch slice {
                    case .all: nil
                    case .active: activity.activeTotal
                    case .attention: activity.attentionTotal
                    case .history: activity.historyTotal
                    }
                },
                identifier: { "task-view-\($0.rawValue)" },
                onSelect: { view = $0 }
            )
            // 刷新时刻行（「x 前更新」）Web 手机端隐藏（max-md:hidden），App 同样不显示；
            // 实时通道状态只作为分隔线的无障碍值留给 UI 测试核对（live-事件数 / poll-事件数）
            Rectangle().fill(Color.white.opacity(0.08)).frame(height: 1)
                .accessibilityElement()
                .accessibilityIdentifier("task-freshness")
                .accessibilityValue("\(store.streamConnected ? "live" : "poll")-\(store.streamEventCount)")
        }
        .padding(.top, 18)
    }

    // MARK: 分区

    private func attentionSection(_ activity: TaskCenter.Activity) -> some View {
        let canDismissAll = activity.standaloneAttentionJobs.contains { $0.status == "failed" }
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 9) {
                Circle().fill(Theme.danger).frame(width: 8, height: 8)
                    .background(Circle().fill(Theme.danger.opacity(0.12)).frame(width: 16, height: 16))
                Text("需要你处理").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.danger)
                Text("\(activity.attentionTotal)")
                    .font(.caption).monospacedDigit().foregroundStyle(Theme.danger)
                    .padding(.horizontal, 8).padding(.vertical, 2)
                    .background(Theme.danger.opacity(0.1), in: .capsule)
                Spacer()
                // 故障常常成批（一次扫描几十个字幕任务一起失败），逐条忽略是灾难；
                // 整体动作压成次要文字按钮，不跟每张卡自己的「重试」抢视觉
                if canDismissAll {
                    Button(bulkDismissing ? "正在忽略…" : "全部忽略") { dismissAllFailed() }
                        .font(.caption.weight(.medium))
                        .foregroundStyle(Theme.textFaint)
                        .buttonStyle(.plain)
                        .disabled(bulkDismissing)
                        .accessibilityIdentifier("dismiss-all-failed")
                }
            }
            VStack(spacing: 10) {
                ForEach(activity.attentionDownloadGroups) { group in
                    DownloadTaskGroupCard(
                        group: group, ingestJobsByHash: activity.ingestJobsByHash,
                        replacingTaskId: replacingTaskId,
                        onDelete: { pendingDelete = $0 }, onReplace: replace
                    )
                }
                ForEach(activity.standaloneAttentionJobs, id: \.id) { job in
                    JobCard(job: job, store: store)
                }
            }
            .padding(12)
            .background(Theme.danger.opacity(0.035), in: .rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Theme.danger.opacity(0.2)))
        }
        .padding(.top, 20)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("attention-section")
    }

    private func activeSection(_ activity: TaskCenter.Activity) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            ActivitySectionHeading(title: "现在", count: activity.activeTotal)
            VStack(spacing: 0) {
                let groups = activity.activeDownloadGroups
                let jobs = activity.standaloneActiveJobs
                ForEach(Array(groups.enumerated()), id: \.element.id) { index, group in
                    TaskTimelineItem(
                        tone: TaskCenter.groupTimelineExecuting(group, ingestJobsByHash: activity.ingestJobsByHash) ? .active : .waiting,
                        isLast: index == groups.count - 1 && jobs.isEmpty
                    ) {
                        DownloadTaskGroupFeed(
                            group: group, ingestJobsByHash: activity.ingestJobsByHash,
                            replacingTaskId: replacingTaskId,
                            onDelete: { pendingDelete = $0 }, onReplace: replace
                        )
                    }
                }
                ForEach(Array(jobs.enumerated()), id: \.element.id) { index, job in
                    TaskTimelineItem(tone: job.status == "running" ? .active : .waiting, isLast: index == jobs.count - 1) {
                        ActiveJobFeedItem(job: job, cancelling: cancellingJobId == job.id) { cancel(job) }
                    }
                }
            }
        }
        .padding(.top, 24)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("active-section")
    }

    private var emptyView: some View {
        let copy: (String, String) = switch view {
        case .all: ("当前没有任务", "新的后台作业或下载任务会自动出现在这里。")
        case .attention: ("当前无需处理", "异常或需要确认的任务会优先出现在这里。")
        case .active: ("当前没有进行中的任务", "新任务启动后会自动进入实时过程。")
        case .history: ("还没有历史记录", "完成、取消，以及被你忽略的后台作业都会保留在这里。")
        }
        return VStack(spacing: 8) {
            Text(copy.0).font(.headline).foregroundStyle(Theme.text.opacity(0.75))
            Text(copy.1).font(.subheadline).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 20)
        .padding(.vertical, 56)
        .background(Color.black.opacity(0.2), in: .rect(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.07)))
        .padding(.top, 28)
        .accessibilityIdentifier("task-empty")
    }

    // MARK: 动作（文案照搬 Web）

    private func replace(_ task: API.DownloadTaskView) {
        guard let downloaderId = task.downloaderId, replacingTaskId == nil else { return }
        replacingTaskId = task.id
        Task {
            defer { replacingTaskId = nil }
            do {
                _ = try await api.dlTorrentReplace(downloaderId: downloaderId, infoHash: task.infoHash)
                feedback.success("已开始寻找同品质替代源；旧任务会保留到新源产生真实进度")
                store.refreshDownloads()
            } catch {
                feedback.error(error.localizedDescription.isEmpty ? "立即换种失败" : error.localizedDescription)
            }
        }
    }

    private func delete(_ task: API.DownloadTaskView, deleteFiles: Bool) async {
        guard let downloaderId = task.downloaderId else { return }
        do {
            let result = try await api.dlTorrentDelete(downloaderId: downloaderId, infoHash: task.infoHash, deleteFiles: deleteFiles)
            feedback.success(result.deleteFiles ? "种子任务和数据文件已删除" : "种子任务已删除，数据文件已保留")
            pendingDelete = nil
            store.refreshDownloads()
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "删除种子任务失败" : error.localizedDescription)
        }
    }

    private func cancel(_ job: API.JobView) {
        guard cancellingJobId == nil else { return }
        cancellingJobId = job.id
        Task {
            defer { cancellingJobId = nil }
            do {
                store.upsert(try await api.jobsCancel(jobId: job.id).job)
                feedback.success("已提交取消请求")
            } catch {
                feedback.error(error.localizedDescription.isEmpty ? "取消任务失败" : error.localizedDescription)
            }
        }
    }

    private func dismissAllFailed() {
        guard !bulkDismissing else { return }
        bulkDismissing = true
        Task {
            defer { bulkDismissing = false }
            do {
                let result = try await api.jobsDismissAll()
                for job in result.jobs { store.upsert(job) }
                feedback.success(result.jobs.isEmpty ? "当前没有需要忽略的失败任务" : "已忽略 \(result.jobs.count) 个失败任务")
            } catch {
                feedback.error(error.localizedDescription.isEmpty ? "批量忽略失败" : error.localizedDescription)
            }
        }
    }

    private func undismiss(_ job: API.JobView) {
        guard undismissingJobId == nil else { return }
        undismissingJobId = job.id
        Task {
            defer { undismissingJobId = nil }
            do {
                store.upsert(try await api.jobsUndismiss(jobId: job.id).job)
                feedback.success("已撤销忽略，任务回到「需要处理」")
            } catch {
                feedback.error(error.localizedDescription.isEmpty ? "撤销忽略失败" : error.localizedDescription)
            }
        }
    }

    private func retry(_ job: API.JobView) {
        guard retryingJobId == nil else { return }
        retryingJobId = job.id
        Task {
            defer { retryingJobId = nil }
            do {
                store.upsert(try await api.jobsRetry(jobId: job.id).job)
                feedback.success("任务已重新加入队列")
            } catch {
                feedback.error(error.localizedDescription.isEmpty ? "重新执行失败" : error.localizedDescription)
            }
        }
    }
}

// MARK: - 时间线

/// 时间线条目：左侧圆点轨道（进行中蓝、等待黄、完成绿勾、取消灰叉），内容与分组标题左对齐
struct TaskTimelineItem<Content: View>: View {
    enum Tone { case active, waiting, success, cancelled }

    var tone: Tone = .active
    var isLast = false
    @ViewBuilder var content: () -> Content

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            ZStack(alignment: .top) {
                if !isLast {
                    Rectangle().fill(Color.white.opacity(0.12)).frame(width: 1).padding(.top, 24)
                }
                dot.padding(.top, 8)
            }
            .frame(width: 20)
            content()
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.bottom, 14)
        }
    }

    @ViewBuilder private var dot: some View {
        switch tone {
        case .success:
            Image(systemName: "checkmark")
                .font(.system(size: 8, weight: .heavy))
                .foregroundStyle(Color(red: 7 / 255, green: 18 / 255, blue: 11 / 255))
                .frame(width: 16, height: 16)
                .background(Theme.success, in: .circle)
        case .cancelled:
            Image(systemName: "xmark")
                .font(.system(size: 8, weight: .heavy))
                .foregroundStyle(Theme.textMuted)
                .frame(width: 16, height: 16)
                .background(Color.white.opacity(0.2), in: .circle)
        case .waiting:
            Circle().fill(Theme.warning).frame(width: 10, height: 10)
                .background(Circle().fill(Theme.warning.opacity(0.25)).frame(width: 16, height: 16))
        case .active:
            Circle().fill(Theme.info).frame(width: 10, height: 10)
                .background(Circle().fill(Theme.info.opacity(0.3)).frame(width: 16, height: 16))
        }
    }
}

/// 「现在」里的后台作业：标题、状态 · 百分比、说明、进度条、处理量、发起方与时刻；可取消
struct ActiveJobFeedItem: View {
    let job: API.JobView
    let cancelling: Bool
    let onCancel: () -> Void

    var body: some View {
        let percent = job.progress.percent
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(TaskCenter.jobFeedIdentity(job) ?? TaskCenter.jobTypeLabel(job))
                        .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.9)).lineLimit(1)
                    HStack(spacing: 5) {
                        Text(TaskCenter.activeJobStatus(job)).fontWeight(.medium).foregroundStyle(Theme.text.opacity(0.7))
                        if let percent {
                            Text("·").foregroundStyle(Theme.textFaint.opacity(0.6))
                            Text("\(Int(percent.rounded()))%").monospacedDigit()
                        }
                    }
                    .font(.footnote)
                    .foregroundStyle(Theme.textMuted)
                }
                Spacer(minLength: 0)
                if job.status != "cancelling" {
                    Button(cancelling ? "正在取消…" : "取消任务", action: onCancel)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(Theme.textFaint)
                        .buttonStyle(.plain)
                        .disabled(cancelling)
                        .accessibilityIdentifier("cancel-job-\(job.id)")
                }
            }
            if !job.progress.message.isEmpty {
                Text(job.progress.message).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(2)
            }
            if percent != nil || job.status == "running" {
                ActivityProgressBar(percent: percent, color: Theme.info).padding(.top, 3)
            }
            if let amount = TaskCenter.feedProgressAmount(job) {
                Text(amount).font(.caption).monospacedDigit().foregroundStyle(Theme.textFaint)
            }
            Text("\(TaskCenter.feedOriginLabel(job)) · \(job.startedAt.map { "\(ActivityFormat.clock($0)) 开始" } ?? "\(ActivityFormat.clock(job.createdAt)) 发起")")
                .font(.caption)
                .foregroundStyle(Theme.textFaint.opacity(0.8))
                .padding(.top, 2)
        }
        .padding(.vertical, 4)
    }
}

// MARK: - 已结束

/// 历史按本地日期分组折叠，首组（或「已结束」视图下的全部组）默认展开
struct TaskHistorySection: View {
    let jobs: [API.JobView]
    let initiallyOpen: Bool
    let separated: Bool
    let retryingJobId: String?
    let undismissingJobId: String?
    let onRetry: (API.JobView) -> Void
    let onUndismiss: (API.JobView) -> Void

    @State private var expanded: Set<String> = []
    @State private var touched = false

    private var groups: [(key: String, label: String, jobs: [API.JobView])] {
        var result: [(key: String, label: String, jobs: [API.JobView])] = []
        for job in jobs {
            let stamp = job.finishedAt ?? job.createdAt
            let key = ActivityFormat.dayKey(stamp)
            if let index = result.firstIndex(where: { $0.key == key }) {
                result[index].jobs.append(job)
            } else {
                result.append((key, ActivityFormat.dayLabel(stamp), [job]))
            }
        }
        return result
    }

    var body: some View {
        let groups = groups
        VStack(alignment: .leading, spacing: 0) {
            if separated { Rectangle().fill(Color.white.opacity(0.1)).frame(height: 1).padding(.top, 20) }
            ForEach(Array(groups.enumerated()), id: \.element.key) { index, group in
                let open = isOpen(group.key, index: index)
                VStack(alignment: .leading, spacing: 0) {
                    Button {
                        toggle(group.key, index: index)
                    } label: {
                        HStack(spacing: 8) {
                            Text(group.label).fontWeight(.semibold)
                            Text("\(group.jobs.count) 项").font(.caption).monospacedDigit().foregroundStyle(Theme.textFaint)
                            Spacer()
                            Text(open ? "收起" : "展开").font(.caption).foregroundStyle(Theme.textFaint)
                            Image(systemName: "chevron.right").font(.caption).rotationEffect(.degrees(open ? 90 : 0))
                        }
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                        .padding(.vertical, 14)
                        .contentShape(.rect)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("history-group-\(index)")
                    if open {
                        VStack(spacing: 0) {
                            ForEach(Array(group.jobs.enumerated()), id: \.element.id) { itemIndex, job in
                                TaskTimelineItem(tone: job.status == "succeeded" ? .success : .cancelled, isLast: itemIndex == group.jobs.count - 1) {
                                    HistoricalJobFeedItem(
                                        job: job, retrying: retryingJobId == job.id, undismissing: undismissingJobId == job.id,
                                        onRetry: { onRetry(job) }, onUndismiss: { onUndismiss(job) }
                                    )
                                }
                            }
                        }
                        .padding(.bottom, 4)
                    }
                    Rectangle().fill(Color.white.opacity(0.07)).frame(height: 1)
                }
            }
        }
        .padding(.top, separated ? 0 : 8)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("history-section")
    }

    private func isOpen(_ key: String, index: Int) -> Bool {
        touched ? expanded.contains(key) : (initiallyOpen || index == 0)
    }

    private func toggle(_ key: String, index: Int) {
        if !touched {
            // 第一次手动展开/收起前，把默认展开的组记下来，后续按用户操作走
            touched = true
            expanded = Set(groups.enumerated().filter { initiallyOpen || $0.offset == 0 }.map(\.element.key))
        }
        if expanded.contains(key) { expanded.remove(key) } else { expanded.insert(key) }
    }
}

/// 历史条目：标题（如「《某剧》入库完成」）、时刻 + 摘要、入库文件明细；忽略的失败可撤销、用户取消的可重新执行
struct HistoricalJobFeedItem: View {
    let job: API.JobView
    let retrying: Bool
    let undismissing: Bool
    let onRetry: () -> Void
    let onUndismiss: () -> Void

    var body: some View {
        let dismissed = job.status == "failed" && TaskCenter.isDismissed(job)
        let ingest = TaskCenter.ingestHistoryDetail(job)
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text(TaskCenter.historicalJobTitle(job))
                    .font(.subheadline.weight(.medium)).foregroundStyle(Theme.text.opacity(0.78)).lineLimit(1)
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(ActivityFormat.clock(job.finishedAt ?? job.createdAt)).monospacedDigit().foregroundStyle(Theme.textFaint.opacity(0.8))
                    Text(ingest?.summary ?? TaskCenter.historicalJobSummary(job)).lineLimit(2)
                }
                .font(.caption)
                .foregroundStyle(Theme.textFaint)
                if let ingest { IngestHistoryFiles(detail: ingest) }
            }
            Spacer(minLength: 0)
            if dismissed {
                Button(undismissing ? "撤销中…" : "撤销忽略", action: onUndismiss)
                    .font(.caption.weight(.medium)).foregroundStyle(Theme.textFaint).buttonStyle(.plain)
                    .disabled(undismissing)
                    .accessibilityIdentifier("undismiss-\(job.id)")
            }
            if job.status == "cancelled", !TaskCenter.isSystemCancelled(job) {
                Button(retrying ? "重新执行中…" : "重新执行", action: onRetry)
                    .font(.caption.weight(.medium)).foregroundStyle(Theme.textFaint).buttonStyle(.plain)
                    .disabled(retrying)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("history-item-\(job.id)")
    }
}

/// 入库历史默认只占一行，需要核对时展开真实文件名与目标媒体库
struct IngestHistoryFiles: View {
    let detail: TaskCenter.IngestHistoryDetail
    @State private var open = false

    var body: some View {
        if !detail.fileGroups.isEmpty || detail.context != nil {
            let fileCount = detail.fileGroups.reduce(0) { $0 + $1.files.count }
            VStack(alignment: .leading, spacing: 6) {
                Button {
                    withAnimation(.snappy(duration: 0.2)) { open.toggle() }
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).rotationEffect(.degrees(open ? 90 : 0))
                        Text("查看文件明细")
                        if fileCount > 0 { Text("\(fileCount) 个").monospacedDigit().foregroundStyle(Theme.textFaint.opacity(0.6)) }
                    }
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    .padding(.vertical, 3)
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                if open {
                    VStack(alignment: .leading, spacing: 8) {
                        if let context = detail.context { Text(context).foregroundStyle(Theme.textFaint) }
                        ForEach(detail.fileGroups, id: \.label) { group in
                            VStack(alignment: .leading, spacing: 3) {
                                Text("\(group.label) · \(group.files.count) 个").font(.caption2.weight(.medium)).foregroundStyle(Theme.textFaint)
                                ForEach(group.files, id: \.self) { file in
                                    Text(file).foregroundStyle(Theme.textMuted).textSelection(.enabled)
                                }
                            }
                        }
                    }
                    .font(.caption)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.white.opacity(0.025), in: .rect(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.06)))
                }
            }
            .padding(.top, 2)
        }
    }
}

// MARK: - 删除种子任务确认

/// 删除种子任务的二次确认：安全默认只移除任务，是否删除数据文件由用户显式勾选
struct DeleteDownloadTaskSheet: View {
    let task: API.DownloadTaskView
    let onConfirm: (Bool) async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var deleteFiles = false
    @State private var busy = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text("将从「\(TaskCenter.nonEmpty(task.downloaderName) ?? "下载器 \(task.downloaderId ?? 0)")」停止并移除该任务。")
                        .font(.subheadline).foregroundStyle(Theme.textMuted)
                    Text(TaskCenter.nonEmpty(task.name) ?? task.infoHash)
                        .font(.subheadline)
                        .foregroundStyle(Theme.text.opacity(0.75))
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.white.opacity(0.035), in: .rect(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
                    Toggle(isOn: $deleteFiles) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("同时删除数据文件").font(.subheadline.weight(.semibold))
                            Text("包括已下载和未完成的数据；若文件仍由下载器管理，即使已经入库也可能被删除，且无法恢复。")
                                .font(.caption).foregroundStyle(Theme.textMuted)
                        }
                    }
                    .tint(Theme.danger)
                    .padding(12)
                    .background(deleteFiles ? Theme.danger.opacity(0.08) : Color.white.opacity(0.025), in: .rect(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(deleteFiles ? Theme.danger.opacity(0.35) : Color.white.opacity(0.08)))
                    .accessibilityIdentifier("delete-files-toggle")
                    if task.source == "subscription" {
                        Text("关联订阅会保留原工单身份；巡检确认任务无进度后会按换源策略寻找替代源。")
                            .font(.caption).foregroundStyle(Color(red: 1, green: 0.95, blue: 0.8).opacity(0.65))
                    }
                    Button(role: .destructive) {
                        busy = true
                        Task {
                            await onConfirm(deleteFiles)
                            busy = false
                        }
                    } label: {
                        Text(busy ? "正在删除…" : deleteFiles ? "删除任务和文件" : "仅删除任务")
                            .font(.body.weight(.semibold))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 4)
                    }
                    .buttonStyle(.glassProminent)
                    .tint(Theme.danger)
                    .disabled(busy)
                    .padding(.top, 6)
                    .accessibilityIdentifier("delete-task-confirm")
                }
                .padding(20)
            }
            .navigationTitle("删除种子任务？")
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
