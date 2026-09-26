import SwiftUI

// 任务中心的卡片：需要处理的 Job 卡 / 下载任务卡（含作品分组）、「现在」时间线里的下载过程行、
// 任务完整过程（生命周期）、季集标签、刷流做种分组。移植自 Web task-center-view.tsx 与 job-center.tsx。

// MARK: - 通用小件

extension Text {
    /// 拼接多段带样式的文字（iOS 26 起 `Text + Text` 已弃用，改用插值）
    static func activityJoin(_ parts: [Text]) -> Text {
        parts.reduce(Text(verbatim: "")) { Text("\($0)\($1)") }
    }
}

/// 上传绿 / 下载蓝的实时速度（全任务中心同一配色语义）
struct SpeedStat: View {
    enum Direction { case up, down }
    let direction: Direction
    let bytesPerSecond: Int?
    var placeholder: String?

    var body: some View {
        if let speed = bytesPerSecond, speed > 0 {
            Text("\(direction == .up ? "↑" : "↓") \(ActivityFormat.rate(Double(speed)))")
                .fontWeight(.semibold)
                .monospacedDigit()
                .foregroundStyle(direction == .up ? Theme.success : Theme.info)
        } else if let placeholder {
            Text(placeholder).monospacedDigit().foregroundStyle(Theme.textFaint.opacity(0.55))
        }
    }
}

/// 小胶囊徽标（「实时」「站点名」「洗版」「已忽略」）
struct TaskTag: View {
    let text: String
    var color: Color = Theme.textMuted
    var fill: Color?

    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(color)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background((fill ?? color.opacity(0.12)), in: .capsule)
            .overlay(Capsule().strokeBorder(color.opacity(0.28)))
    }
}

/// 洗版任务的身份标（青色，与订阅详情页洗版专属色一致）
struct UpgradeTaskBadge: View {
    let task: API.DownloadTaskView

    var body: some View {
        if TaskCenter.isUpgradeTask(task) {
            TaskTag(text: "洗版", color: Color(red: 0x2D / 255, green: 0xD4 / 255, blue: 0xBF / 255))
                .accessibilityHint("洗版任务：这些集库里已有旧版本，本次下载完成并校验后替换")
        }
    }
}

/// 右上角「⋯」：打开种子页 / 删除任务
private struct DownloadTaskActionsMenu: View {
    let task: API.DownloadTaskView
    let busy: Bool
    let onDelete: (API.DownloadTaskView) -> Void
    @Environment(\.openURL) private var openURL

    var body: some View {
        if task.pageUrl != nil || task.downloaderId != nil {
            Menu {
                if let page = task.pageUrl, let url = URL(string: page) {
                    Button("打开种子页", systemImage: "safari") { openURL(url) }
                }
                if task.downloaderId != nil {
                    Button("删除任务", systemImage: "trash", role: .destructive) { onDelete(task) }
                }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.textMuted)
                    .frame(width: 30, height: 28)
                    .contentShape(.rect)
            }
            .disabled(busy)
            .accessibilityLabel("\(TaskCenter.nonEmpty(task.name) ?? task.infoHash)的更多操作")
            .accessibilityIdentifier("download-actions-\(task.infoHash)")
        }
    }
}

// MARK: - 季集标签

/// 多集资源：收起态显示连续区间与入库进度，点开按季列出完整集号（已入库/已替换的标绿）
struct EpisodeUnitsLabel: View {
    let units: [API.DownloadTaskUnitView]
    let purpose: String
    @State private var open = false

    var body: some View {
        if let summary = TaskCenter.summarizeEpisodeUnits(units) {
            let upgrade = purpose == "upgrade"
            let doneKeys = Set(units.filter { upgrade ? $0.replaced : $0.status == "imported" }.map { "\($0.seasonNumber):\($0.episodeNumber)" })
            let doneWord = upgrade ? "已替换" : "已入库"
            if summary.isMovie || summary.episodeCount <= 1 {
                HStack(spacing: 0) {
                    Text(summary.label)
                    if !summary.isMovie, !doneKeys.isEmpty { Text("（\(doneWord)）").foregroundStyle(Theme.success) }
                }
                .font(.caption).monospacedDigit().foregroundStyle(Theme.text.opacity(0.88))
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    Button {
                        withAnimation(.snappy(duration: 0.2)) { open.toggle() }
                    } label: {
                        HStack(spacing: 4) {
                            Text(summary.label).lineLimit(1)
                            if !doneKeys.isEmpty {
                                Text(upgrade ? "（已完成 \(doneKeys.count) 集替换）" : "（有 \(doneKeys.count) 集已入库）")
                                    .foregroundStyle(Theme.success).fixedSize()
                            }
                            Image(systemName: "chevron.down").font(.system(size: 9, weight: .semibold)).opacity(0.45)
                                .rotationEffect(.degrees(open ? 180 : 0))
                        }
                        .font(.caption).monospacedDigit().foregroundStyle(Theme.text.opacity(0.88))
                        .expandedHitArea(vertical: 12)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("覆盖 \(summary.episodeCount) 集：\(summary.fullLabel)。展开查看全部集号")
                    if open {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(summary.seasons, id: \.season) { season in
                                VStack(alignment: .leading, spacing: 6) {
                                    HStack(spacing: 6) {
                                        Text("S\(TaskCenter.pad2(season.season))").fontWeight(.medium).foregroundStyle(Theme.textMuted)
                                        Text("共 \(season.episodes.count) 集").foregroundStyle(Theme.textFaint)
                                    }
                                    DiscoverFlowLayout(spacing: 5, lineSpacing: 5) {
                                        ForEach(season.episodes, id: \.self) { episode in
                                            let done = doneKeys.contains("\(season.season):\(episode)")
                                            Text("E\(TaskCenter.pad2(episode))")
                                                .font(.system(size: 11)).monospacedDigit()
                                                .foregroundStyle(done ? Theme.success : Theme.textMuted)
                                                .padding(.horizontal, 5).padding(.vertical, 2)
                                                .background(done ? Theme.success.opacity(0.16) : Color.white.opacity(0.055), in: .rect(cornerRadius: 5))
                                                .accessibilityLabel(done ? "E\(episode) \(doneWord)" : "E\(episode)")
                                        }
                                    }
                                }
                            }
                            if summary.seasons.count > 1 {
                                Text("合计 \(summary.episodeCount) 集").font(.caption2).foregroundStyle(Theme.textFaint)
                                    .frame(maxWidth: .infinity, alignment: .trailing)
                            }
                        }
                        .font(.caption)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.white.opacity(0.035), in: .rect(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.07)))
                    }
                }
            }
        }
    }
}

// MARK: - 任务完整过程

/// 已投递 → 下载 → 入库/搬运/替换……每一步都能回溯到下载快照或 Job；竖排（手机）
struct DownloadLifecycleView: View {
    let task: API.DownloadTaskView
    let ingestJob: API.JobView?
    var feed = false

    var body: some View {
        let steps = TaskCenter.lifecycleSteps(task, ingestJob: ingestJob)
        VStack(alignment: .leading, spacing: 0) {
            if !feed { Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1).padding(.bottom, 10) }
            ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                HStack(alignment: .top, spacing: 8) {
                    ZStack(alignment: .top) {
                        if index < steps.count - 1 {
                            Rectangle().fill(Color.white.opacity(0.1)).frame(width: 1).padding(.top, 12)
                        }
                        stepDot(step.tone).padding(.top, 5)
                    }
                    .frame(width: 8)
                    VStack(alignment: .leading, spacing: 1) {
                        if feed {
                            Text.activityJoin([
                                Text(step.label).fontWeight(.medium).foregroundStyle(labelColor(step.tone)),
                                Text("  "), Text(step.detail).foregroundStyle(detailColor(step.tone)),
                            ])
                                .font(.caption)
                                .lineLimit(step.tone == .attention ? 3 : 2)
                        } else {
                            Text(step.label).font(.caption.weight(.medium)).foregroundStyle(labelColor(step.tone))
                            if !step.detail.isEmpty {
                                Text(step.detail).font(.caption2).foregroundStyle(detailColor(step.tone))
                                    .lineLimit(step.tone == .attention ? 3 : 2)
                            }
                        }
                    }
                    .padding(.bottom, 7)
                }
            }
        }
        .padding(.top, 10)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("任务完整过程")
    }

    @ViewBuilder private func stepDot(_ tone: TaskCenter.StepTone) -> some View {
        switch tone {
        case .done: Circle().fill(Theme.success).frame(width: 8, height: 8)
        case .current: ActivityStatusDot(color: Theme.info, pulse: true, size: 8)
        case .waiting: Circle().fill(Theme.warning).frame(width: 8, height: 8)
        case .attention: Circle().fill(Theme.danger).frame(width: 8, height: 8)
        case .future: Circle().strokeBorder(Color.white.opacity(0.25)).frame(width: 8, height: 8)
        }
    }

    private func labelColor(_ tone: TaskCenter.StepTone) -> Color {
        switch tone {
        case .done: Theme.text.opacity(0.6)
        case .current: Theme.info
        case .waiting: Color(red: 1, green: 0.95, blue: 0.8).opacity(0.7)
        case .attention: Theme.danger
        case .future: Theme.textFaint
        }
    }

    private func detailColor(_ tone: TaskCenter.StepTone) -> Color {
        switch tone {
        case .waiting: Color(red: 1, green: 0.95, blue: 0.8).opacity(0.45)
        case .attention: Theme.danger.opacity(0.75)
        default: Theme.textFaint.opacity(0.85)
        }
    }
}

// MARK: - 需要处理：下载任务卡

/// 需要处理区的作品分组卡：顶部作品身份（海报、类型、资源数、查看订阅），下面逐个资源卡
struct DownloadTaskGroupCard: View {
    let group: TaskCenter.DownloadGroup
    let ingestJobsByHash: [String: API.JobView]
    let replacingTaskId: String?
    let onDelete: (API.DownloadTaskView) -> Void
    let onReplace: (API.DownloadTaskView) -> Void

    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    var body: some View {
        if group.mediaItemId == nil, let task = group.tasks.first {
            DownloadTaskCard(
                task: task, ingestJob: ingestJobsByHash[task.infoHash.lowercased()],
                replacing: replacingTaskId == task.id, onDelete: onDelete, onReplace: onReplace
            )
        } else {
            let subscription = group.tasks.flatMap(\.subscriptions).first
            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    RemoteImage(url: api.image(group.posterUrl, .posterCard), placeholderSymbol: group.kind == "tv" ? "tv" : "film")
                        .frame(width: 36, height: 54)
                        .clipShape(.rect(cornerRadius: 7))
                        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.white.opacity(0.1)))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(group.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.9)).lineLimit(1)
                        Text("\(group.kind == "tv" ? "剧集" : "电影") · \(group.tasks.count) 个下载资源")
                            .font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    Spacer(minLength: 0)
                    if let subscription {
                        Button("查看订阅") { router.open(.subscription(id: subscription.id)) }
                            .font(.caption.weight(.medium))
                            .buttonStyle(.glass)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                Rectangle().fill(Color.white.opacity(0.07)).frame(height: 1)
                VStack(spacing: 8) {
                    ForEach(group.tasks) { task in
                        DownloadTaskCard(
                            task: task, ingestJob: ingestJobsByHash[task.infoHash.lowercased()], grouped: true,
                            replacing: replacingTaskId == task.id, onDelete: onDelete, onReplace: onReplace
                        )
                    }
                }
                .padding(12)
            }
            .background(Color(red: 14 / 255, green: 16 / 255, blue: 22 / 255).opacity(0.52), in: .rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.08)))
        }
    }
}

/// 一条下载任务的完整卡片（需要处理区）：状态点 + 标题、原因说明（红框）+ 立即换种、
/// 覆盖集号、来源 / 下载器 / 体积、进度（下载或入库）、任务完整过程、底部出口（交给 AI 分析 / 删除下载任务）
struct DownloadTaskCard: View {
    let task: API.DownloadTaskView
    let ingestJob: API.JobView?
    var grouped = false
    let replacing: Bool
    let onDelete: (API.DownloadTaskView) -> Void
    let onReplace: (API.DownloadTaskView) -> Void

    var body: some View {
        let ingestOwns = TaskCenter.ingestOwnsTaskState(task, ingestJob: ingestJob)
        let meta = ingestOwns && ingestJob != nil ? TaskCenter.ingestStateMeta(ingestJob!.status) : TaskCenter.downloadStateMeta(task.state)
        let title = grouped
            ? (TaskCenter.nonEmpty(task.name) ?? TaskCenter.nonEmpty(task.mediaTitle) ?? task.infoHash)
            : (TaskCenter.nonEmpty(task.mediaTitle) ?? TaskCenter.nonEmpty(task.name) ?? task.infoHash)
        let torrentName = !grouped && task.name != nil && task.name != title ? task.name : nil
        let needsAttention = TaskCenter.downloadTaskNeedsAttention(task, ingestJob: ingestJob)
        let note = TaskCenter.downloadTaskNote(task, ingestJob: ingestJob)

        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                ActivityStatusDot(color: meta.color, pulse: meta.pulse, size: 10, label: meta.label).padding(.top, 5)
                Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.9)).lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                DownloadTaskActionsMenu(task: task, busy: replacing, onDelete: onDelete).padding(.top, -4)
            }
            if torrentName != nil || note != nil {
                noteBox(torrentName: torrentName, note: note, attention: needsAttention)
            }
            if let subscription = task.subscriptions.first {
                HStack(alignment: .top, spacing: 8) {
                    UpgradeTaskBadge(task: task)
                    EpisodeUnitsLabel(units: subscription.units, purpose: subscription.purpose)
                }
            }
            HStack(spacing: 12) {
                Text(TaskCenter.sourceLabel(task))
                Text(TaskCenter.nonEmpty(task.downloaderName) ?? "所有下载器均未找到")
                if let size = task.sizeBytes { Text(ActivityFormat.bytes(Double(size))) }
            }
            .font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
            progress(ingestOwns: ingestOwns, color: meta.color)
            DownloadLifecycleView(task: task, ingestJob: ingestJob)
            if needsAttention {
                footer
            }
        }
        .padding(grouped ? 12 : 14)
        .background(grouped ? Color.black.opacity(0.15) : Color(red: 14 / 255, green: 16 / 255, blue: 22 / 255).opacity(0.5),
                    in: .rect(cornerRadius: grouped ? 12 : 16))
        .overlay(RoundedRectangle(cornerRadius: grouped ? 12 : 16).strokeBorder(Color.white.opacity(grouped ? 0.06 : 0.08)))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("download-card-\(task.infoHash)")
    }

    private func noteBox(torrentName: String?, note: String?, attention: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text.activityJoin([
                attention ? Text("需要处理：").fontWeight(.semibold) : nil,
                torrentName.map { Text($0) },
                torrentName != nil && note != nil ? Text(" · ") : nil,
                note.map { Text($0) },
            ].compactMap { $0 })
            .font(.footnote)
            .lineLimit(3)
            .textSelection(.enabled)
            if TaskCenter.shouldOfferInlineReplacement(task) {
                Button { onReplace(task) } label: {
                    Text(replacing ? "正在换种…" : "立即换种")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color(red: 1, green: 0.89, blue: 0.89))
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(Theme.danger.opacity(0.1), in: .rect(cornerRadius: 8))
                        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.danger.opacity(0.3)))
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .disabled(replacing)
                .accessibilityIdentifier("replace-\(task.infoHash)")
            }
        }
        .foregroundStyle(attention ? Theme.danger : Theme.textMuted)
        .padding(attention ? 11 : 0)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(attention ? Theme.danger.opacity(0.06) : .clear, in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(attention ? Theme.danger.opacity(0.15) : .clear))
    }

    @ViewBuilder
    private func progress(ingestOwns: Bool, color: Color) -> some View {
        let downloadPercent = task.progress.map { Int(($0 * 100).rounded(.down)) }
        let percent: Int? = ingestOwns ? ingestJob?.progress.percent.map { Int($0.rounded(.down)) } : downloadPercent
        if let percent, task.state != "missing" {
            let runtime = !ingestOwns && task.state == "downloading"
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text(ingestOwns ? "入库进度" : "下载进度").fontWeight(.medium).foregroundStyle(Theme.textMuted)
                    if runtime, let speed = task.dlspeedBytes, speed > 0 {
                        Text("·").foregroundStyle(Theme.textFaint.opacity(0.6))
                        SpeedStat(direction: .down, bytesPerSecond: speed)
                    }
                    if runtime, let eta = task.etaSeconds {
                        Text("·").foregroundStyle(Theme.textFaint.opacity(0.6))
                        Text("剩余约 \(ActivityFormat.duration(seconds: Double(eta)))").foregroundStyle(Theme.textFaint)
                    }
                    Spacer()
                    Text("\(min(100, max(0, percent)))%").fontWeight(.semibold).monospacedDigit().foregroundStyle(Theme.text.opacity(0.65))
                }
                .font(.caption)
                ActivityProgressBar(percent: Double(percent), color: color, track: Color.white.opacity(0.08))
            }
        }
    }

    /// 需要处理的任务一律给「交给 AI 分析」作为第二出口；下载器里这条任务出错或 movieclaw 看不到文件时给删除
    private var footer: some View {
        VStack(spacing: 10) {
            Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1)
            HStack(spacing: 8) {
                Spacer()
                ActivityHandoffButton(kind: "download", refId: task.infoHash)
                if task.state == "error" || task.landingError != nil, task.downloaderId != nil {
                    Button("删除下载任务") { onDelete(task) }
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.danger)
                        .buttonStyle(.glass)
                }
            }
        }
    }
}

// MARK: - 现在：下载过程行

/// 「现在」里的作品组：有媒体条目时带组头（实时 · 片名 · 类型 · 资源数 · 查看订阅），组内逐个资源
struct DownloadTaskGroupFeed: View {
    let group: TaskCenter.DownloadGroup
    let ingestJobsByHash: [String: API.JobView]
    let replacingTaskId: String?
    let onDelete: (API.DownloadTaskView) -> Void
    let onReplace: (API.DownloadTaskView) -> Void

    @Environment(Router.self) private var router

    var body: some View {
        let grouped = group.mediaItemId != nil
        let subscription = group.tasks.flatMap(\.subscriptions).first
        VStack(alignment: .leading, spacing: 8) {
            if grouped {
                HStack(alignment: .top, spacing: 10) {
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 7) {
                            TaskTag(text: "实时", color: Theme.info)
                            Text(group.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.9)).lineLimit(1)
                        }
                        Text("\(group.kind == "tv" ? "剧集" : "电影") · \(group.tasks.count) 个下载资源")
                            .font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    Spacer(minLength: 0)
                    if let subscription {
                        Button { router.open(.subscription(id: subscription.id)) } label: {
                            Text("查看订阅")
                                .font(.caption.weight(.medium))
                                .foregroundStyle(Theme.textFaint)
                                .expandedHitArea(vertical: 14)
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("view-subscription-\(subscription.id)")
                    }
                }
            }
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(group.tasks.enumerated()), id: \.element.id) { index, task in
                    if index > 0 { Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1).padding(.vertical, 8) }
                    DownloadTaskFeedItem(
                        task: task, ingestJob: ingestJobsByHash[task.infoHash.lowercased()], grouped: grouped,
                        replacing: replacingTaskId == task.id, onDelete: onDelete, onReplace: onReplace
                    )
                }
            }
            .padding(.leading, grouped ? 13 : 0)
            .overlay(alignment: .leading) {
                if grouped { Rectangle().fill(Color.white.opacity(0.1)).frame(width: 1) }
            }
        }
        .padding(.vertical, 4)
    }
}

/// 一个下载资源的过程行：站点徽标 + 种子名、覆盖集号、规格、说明、进度、实时速度与剩余时间、完整过程
struct DownloadTaskFeedItem: View {
    let task: API.DownloadTaskView
    let ingestJob: API.JobView?
    let grouped: Bool
    let replacing: Bool
    let onDelete: (API.DownloadTaskView) -> Void
    let onReplace: (API.DownloadTaskView) -> Void

    var body: some View {
        let ingestOwns = TaskCenter.ingestOwnsTaskState(task, ingestJob: ingestJob)
        let meta = ingestOwns && ingestJob != nil ? TaskCenter.ingestStateMeta(ingestJob!.status) : TaskCenter.downloadStateMeta(task.state)
        let downloadPercent = task.progress.map { Int(($0 * 100).rounded(.down)) }
        let percent: Int? = ingestOwns ? ingestJob?.progress.percent.map { Int($0.rounded(.down)) } : downloadPercent
        let title = grouped
            ? (TaskCenter.nonEmpty(task.name) ?? TaskCenter.nonEmpty(task.mediaTitle) ?? task.infoHash)
            : (TaskCenter.nonEmpty(task.mediaTitle) ?? TaskCenter.nonEmpty(task.name) ?? task.infoHash)
        // 有关联入库 Job 时入库阶段由完整过程承担，过程行只保留下载态 / 换源提示
        let note = ingestJob == nil ? TaskCenter.downloadTaskNote(task, ingestJob: nil) : nil
        let specs = [task.resolution, task.mediaSource, task.remux ? "Remux" : nil, task.sizeBytes.map { ActivityFormat.bytes(Double($0)) }]
            .compactMap { $0 }
        let downSpeed = !ingestOwns && task.state == "downloading" ? task.dlspeedBytes.flatMap { $0 > 0 ? $0 : nil } : nil
        let upSpeed = task.upspeedBytes.flatMap { $0 > 0 ? $0 : nil }
        let eta = !ingestOwns ? task.etaSeconds : nil

        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 8) {
                HStack(spacing: 7) {
                    if !grouped { TaskTag(text: "实时", color: Theme.info) }
                    if let site = task.siteName { TaskTag(text: site, color: Theme.textMuted, fill: Color.white.opacity(0.06)) }
                    Text(title).font((grouped ? Font.footnote : .subheadline).weight(.semibold))
                        .foregroundStyle(Theme.text.opacity(0.88)).lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                DownloadTaskActionsMenu(task: task, busy: replacing, onDelete: onDelete).padding(.top, -4)
            }
            if let subscription = task.subscriptions.first {
                HStack(alignment: .top, spacing: 8) {
                    UpgradeTaskBadge(task: task)
                    EpisodeUnitsLabel(units: subscription.units, purpose: subscription.purpose)
                }
            }
            if !specs.isEmpty {
                Text(specs.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.text.opacity(0.88)).lineLimit(1)
            }
            if let note {
                Text(note).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(2)
            }
            if let percent, task.state != "missing" {
                ActivityProgressBar(percent: Double(percent), color: meta.color).padding(.top, 4)
                    .accessibilityLabel("\(meta.label) \(percent)%")
            }
            if downSpeed != nil || upSpeed != nil || eta != nil {
                HStack(spacing: 10) {
                    SpeedStat(direction: .down, bytesPerSecond: downSpeed)
                    SpeedStat(direction: .up, bytesPerSecond: upSpeed)
                    if let eta { Text("剩余约 \(ActivityFormat.duration(seconds: Double(eta)))") }
                }
                .font(.caption)
                .foregroundStyle(Theme.textFaint)
            }
            DownloadLifecycleView(task: task, ingestJob: ingestJob, feed: true)
            if TaskCenter.shouldOfferInlineReplacement(task) {
                Button { onReplace(task) } label: {
                    Text(replacing ? "正在换种…" : "立即换种")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color(red: 1, green: 0.89, blue: 0.89))
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(Theme.danger.opacity(0.07), in: .rect(cornerRadius: 8))
                        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.danger.opacity(0.25)))
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .disabled(replacing)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("download-feed-\(task.infoHash)")
    }
}

// MARK: - 刷流做种

/// 刷流做种的实时汇总（↑/↓ 总速度、已上传、已下载）：总览的一行与二级页页头共用。
/// 刷流没有入库流转语义，不进时间线、不参与计数，也不提供删除入口（汰换归引擎管）。
struct ActivityBoostTotals {
    var count: Int
    var upSpeed: Int
    var downSpeed: Int
    var uploaded: Int
    var downloaded: Int

    init(_ tasks: [API.DownloadTaskView]) {
        count = tasks.count
        upSpeed = tasks.reduce(0) { $0 + ($1.upspeedBytes ?? 0) }
        downSpeed = tasks.reduce(0) { $0 + ($1.dlspeedBytes ?? 0) }
        uploaded = tasks.reduce(0) { $0 + ($1.uploadedBytes ?? 0) }
        downloaded = tasks.reduce(0) { $0 + ($1.completedBytes ?? 0) }
    }

    /// 按上行速度倒序（正在出力的浮在最前），同速按累计上传
    static func sorted(_ tasks: [API.DownloadTaskView]) -> [API.DownloadTaskView] {
        tasks.sorted { (($0.upspeedBytes ?? 0), ($0.uploadedBytes ?? 0)) > (($1.upspeedBytes ?? 0), ($1.uploadedBytes ?? 0)) }
    }
}

/// 刷流种子按来源站点的开关状态分组。
///
/// 关掉刷流不会删种：已下好的种子留在下载器里继续满速做种（暂停只是每种限速 1 KiB/s），
/// 引擎也不再汰换它们——所以「还有刷流种子」不等于「刷流开着」，总览与刷流页都按站点把状态说清楚。
/// 状态来自 `GET /sites/boost-pool`（在池概况）；`pool` 为 nil（还没取到或取失败）时一律当作
/// 运行中，不替用户下「已关闭」的结论。
struct ActivityBoostSites {
    enum Mode { case running, paused, off }

    struct Site: Identifiable {
        var id: String
        var name: String
        var mode: Mode
        var tasks: [API.DownloadTaskView]
        /// 后端的在池概况（保留期、待清理数）；pool 没取到时为 nil
        var pool: API.BoostPoolSiteView?
    }

    let sites: [Site]
    /// infohash → 清理状态（保留期到期时刻、是否已请求清理）
    let taskStates: [String: API.BoostPoolTaskView]

    init(tasks: [API.DownloadTaskView], pool: API.BoostPoolView?) {
        let bySite = Dictionary(grouping: tasks) { $0.siteId ?? "" }
        let poolSites = Dictionary((pool?.sites ?? []).map { ($0.siteId, $0) }, uniquingKeysWith: { first, _ in first })
        sites = bySite.map { siteId, tasks in
            let info = poolSites[siteId]
            let mode: Mode = if pool == nil {
                .running
            } else if let info, info.boostEnabled {
                info.boostPaused ? .paused : .running
            } else {
                .off
            }
            let name = tasks.first?.siteName ?? info?.siteName ?? (siteId.isEmpty ? "未知站点" : siteId)
            return Site(id: siteId, name: name, mode: mode, tasks: tasks, pool: info)
        }
        .sorted { $0.tasks.count > $1.tasks.count }
        taskStates = Dictionary((pool?.tasks ?? []).map { ($0.infoHash.lowercased(), $0) }, uniquingKeysWith: { first, _ in first })
    }

    func count(_ mode: Mode) -> Int {
        sites.filter { $0.mode == mode }.reduce(0) { $0 + $1.tasks.count }
    }

    /// 已请求清理、等着自动删除的种子数
    var scheduledCount: Int { taskStates.values.filter(\.cleanupScheduled).count }
}

/// 读刷流在池概况。旧版服务端没有 `GET /sites/boost-pool`（App 可能比服务端新）时退回站点列表，
/// 只拿各站开关 / 暂停状态——没有保留期与清理信息，`supportsCleanup=false`，清理入口不出现。
enum ActivityBoostPoolLoader {
    static func load(_ api: APIClient) async -> (pool: API.BoostPoolView, supportsCleanup: Bool)? {
        if let pool = try? await api.siteBoostPoolShow() { return (pool, true) }
        guard let sites = try? await api.siteList() else { return nil }
        let fallback = API.BoostPoolView(
            sites: sites.map {
                API.BoostPoolSiteView(
                    siteId: $0.siteId, siteName: $0.siteId, boostEnabled: $0.boostEnabled, boostPaused: $0.boostPaused,
                    taskCount: 0, sizeBytes: 0, deletableCount: 0, deletableBytes: 0,
                    protectedCount: 0, protectedBytes: 0, protectedUntil: nil, scheduledCount: 0
                )
            },
            tasks: []
        )
        return (fallback, false)
    }
}

/// 刷流清理的文案（活动页刷流做种、设置页关闭刷流两处共用）
enum ActivityBoostCleanupText {
    /// 「9月29日 14:00」
    static func deadline(_ raw: String?) -> String? {
        guard let date = Formatters.date(raw) else { return nil }
        let c = Calendar(identifier: .gregorian).dateComponents([.month, .day, .hour, .minute], from: date)
        return String(format: "%d月%d日 %02d:%02d", c.month ?? 0, c.day ?? 0, c.hour ?? 0, c.minute ?? 0)
    }

    /// 清理结果的一句话反馈
    static func summary(_ result: API.BoostCleanupResult) -> String {
        var parts: [String] = []
        if result.deletedCount > 0 {
            parts.append("已删除 \(result.deletedCount) 个刷流种子，释放 \(ActivityFormat.bytes(Double(result.deletedBytes)))")
        }
        if result.scheduledCount > 0 {
            let until = deadline(result.scheduledUntil).map { "（最晚 \($0)）" } ?? ""
            parts.append("\(result.scheduledCount) 个还在保留期内，到期后自动删除\(until)")
        }
        if result.failedCount > 0 {
            parts.append("\(result.failedCount) 个因下载器暂时连不上没删成，稍后自动重试")
        }
        return parts.isEmpty ? "没有需要清理的刷流种子" : parts.joined(separator: "；")
    }
}

/// 刷流单行：站点 + 名称（可点开种子页）占一行，数字列（↑速度 / 累计上传 / 体积）另起一行逐行对齐；
/// 下载中的少数种子再补一行进度与下行速度
struct BoostTaskRow: View {
    let task: API.DownloadTaskView
    /// 清理备注（「已请求清理 · 9月29日 14:00 后自动删除」）；nil = 不显示
    var cleanupNote: String?
    @Environment(\.openURL) private var openURL

    var body: some View {
        let downloading = task.state == "downloading"
        let percent = task.progress.map { Int(($0 * 100).rounded(.down)) }
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                if let site = task.siteName {
                    Text(site).font(.caption.weight(.medium)).foregroundStyle(Theme.textMuted).lineLimit(1)
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(Color.white.opacity(0.06), in: .capsule)
                        .frame(maxWidth: 80, alignment: .leading)
                }
                Button {
                    if let page = task.pageUrl, let url = URL(string: page) { openURL(url) }
                } label: {
                    Text(TaskCenter.nonEmpty(task.name) ?? task.infoHash)
                        .font(.subheadline).foregroundStyle(Theme.text.opacity(0.8)).lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .disabled(task.pageUrl == nil)
            }
            Grid(horizontalSpacing: 8) {
                GridRow {
                    SpeedStat(direction: .up, bytesPerSecond: task.upspeedBytes, placeholder: "—")
                        .gridColumnAlignment(.trailing).frame(maxWidth: .infinity, alignment: .trailing)
                    Text(task.uploadedBytes.flatMap { $0 > 0 ? "累计 ↑\(ActivityFormat.bytes(Double($0)))" : nil } ?? "—")
                        .frame(maxWidth: .infinity, alignment: .trailing)
                    Text(task.sizeBytes.map { ActivityFormat.bytes(Double($0)) } ?? "—")
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
            .font(.caption)
            .monospacedDigit()
            .lineLimit(1)
            .foregroundStyle(Theme.textFaint)
            if (downloading && percent != nil) || (task.dlspeedBytes ?? 0) > 0 {
                HStack(spacing: 8) {
                    Spacer()
                    if downloading, let percent { Text("\(percent)%").monospacedDigit() }
                    SpeedStat(direction: .down, bytesPerSecond: task.dlspeedBytes)
                }
                .font(.caption)
                .foregroundStyle(Theme.textFaint)
            }
            if let cleanupNote {
                Label(cleanupNote, systemImage: "clock.badge.xmark")
                    .font(.caption)
                    .foregroundStyle(Theme.warning)
            }
        }
        .padding(.vertical, 8)
    }
}
