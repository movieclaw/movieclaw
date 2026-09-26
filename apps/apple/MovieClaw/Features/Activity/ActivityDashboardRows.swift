import Charts
import SwiftUI

// 活动总览（系统分组列表）里除「正在播放」外的各种行。
// 共同约定：行只露一眼能看懂的摘要（标题 · 一行状态 · 进度条），完整过程与全部操作在二级页；
// 整行是 NavigationLink（系统的按压高亮与右箭头），常用操作放左滑，不在行上堆按钮。

/// 分组标题：「● 标题 数量 …… 查看全部 ›」。圆点只给需要处理（红）与正在播放（绿）这两类「此刻」分组
struct ActivitySectionHeader: View {
    let title: String
    var count: Int?
    var tint: Color?
    var trailing: String?
    var trailingIdentifier: String?
    /// 「查看全部」带右箭头（去往下一页）；「全部忽略」这类就地动作不带
    var chevron = true
    var action: (() -> Void)?

    var body: some View {
        HStack(spacing: 6) {
            if let tint { Circle().fill(tint).frame(width: 8, height: 8) }
            Text(title).foregroundStyle(Theme.text)
            if let count { Text("\(count)").foregroundStyle(Theme.textFaint).monospacedDigit() }
            Spacer(minLength: 8)
            if let trailing, let action {
                Button(action: action) {
                    HStack(spacing: 3) {
                        Text(trailing)
                        if chevron { Image(systemName: "chevron.right").font(.caption.weight(.semibold)) }
                    }
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .expandedHitArea(vertical: 10)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier(trailingIdentifier ?? "")
            }
        }
        .textCase(nil)
    }
}

// MARK: - 进行中

/// 进行中的下载（按作品合并的一组）：下载完成后改报入库进度——下载与入库是同一件事的两段
struct ActivityActiveDownloadRow: View {
    let group: TaskCenter.DownloadGroup
    let ingestJobsByHash: [String: API.JobView]

    var body: some View {
        let task = group.tasks[0]
        let ingest = task.state == "completed" ? ingestJobsByHash[task.infoHash.lowercased()] : nil
        let meta = ingest.map { TaskCenter.ingestStateMeta($0.status) } ?? TaskCenter.downloadStateMeta(task.state)
        let percent: Double? = if let ingest { ingest.progress.percent } else { task.progress.map { $0 * 100 } }
        HStack(spacing: 12) {
            Image(systemName: symbol(task.state, ingesting: ingest != nil))
                .font(.body.weight(.medium)).foregroundStyle(meta.color).frame(width: 26)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(group.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                    if group.tasks.count > 1 {
                        Text("\(group.tasks.count) 个资源").font(.caption).foregroundStyle(Theme.textFaint).fixedSize()
                    }
                    Spacer(minLength: 4)
                    if let percent {
                        Text("\(Int(percent.rounded(.down)))%").font(.footnote.weight(.semibold)).monospacedDigit().foregroundStyle(Theme.textMuted)
                    }
                }
                Text(statusLine(task, ingest: ingest, label: meta.label))
                    .font(.footnote).monospacedDigit().foregroundStyle(Theme.textMuted).lineLimit(1)
                if let percent {
                    ProgressView(value: min(100, max(0, percent)), total: 100).tint(meta.color).padding(.top, 2)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func symbol(_ state: String, ingesting: Bool) -> String {
        if ingesting { return "tray.and.arrow.down" }
        switch state {
        case "downloading": return "arrow.down"
        case "paused", "stalled": return "pause"
        case "checking": return "arrow.triangle.2.circlepath"
        case "completed": return "tray.and.arrow.down"
        default: return "clock"
        }
    }

    /// 「下载中 · ↓ 12.4 MB/s · 剩 6 分钟」；入库时「正在入库 · 整理文件」
    private func statusLine(_ task: API.DownloadTaskView, ingest: API.JobView?, label: String) -> String {
        var parts = [label]
        if let ingest {
            if !ingest.progress.message.isEmpty { parts.append(ingest.progress.message) }
        } else {
            if let speed = task.dlspeedBytes, speed > 0 { parts.append("↓ \(ActivityFormat.rate(Double(speed)))") }
            if let eta = task.etaSeconds, eta > 0, task.state == "downloading" {
                parts.append("剩 \(ActivityFormat.duration(seconds: Double(eta)))")
            }
        }
        return parts.joined(separator: " · ")
    }
}

/// 进行中的后台作业（扫描、生成字幕、整理入库……）
struct ActivityActiveJobRow: View {
    let job: API.JobView

    var body: some View {
        let percent = job.progress.percent
        HStack(spacing: 12) {
            Image(systemName: "gearshape.2").font(.body.weight(.medium)).foregroundStyle(Theme.info).frame(width: 26)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(TaskCenter.jobFeedIdentity(job) ?? TaskCenter.jobTypeLabel(job))
                        .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                    Spacer(minLength: 4)
                    if let percent {
                        Text("\(Int(percent.rounded()))%").font(.footnote.weight(.semibold)).monospacedDigit().foregroundStyle(Theme.textMuted)
                    }
                }
                Text(WatchFormat.metaLine([TaskCenter.activeJobStatus(job), job.progress.message]))
                    .font(.footnote).foregroundStyle(Theme.textMuted).lineLimit(1)
                if let percent {
                    ProgressView(value: min(100, max(0, percent)), total: 100).tint(Theme.info).padding(.top, 2)
                }
            }
        }
        .padding(.vertical, 4)
    }
}

/// 刷流做种一行：实时汇总，点进二级页看逐种子明细
struct ActivityBoostSummaryRow: View {
    let tasks: [API.DownloadTaskView]

    var body: some View {
        let totals = ActivityBoostTotals(tasks)
        HStack(spacing: 12) {
            Image(systemName: "leaf.fill").foregroundStyle(Theme.success).frame(width: 26)
            VStack(alignment: .leading, spacing: 3) {
                Text("刷流做种").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                Text(WatchFormat.metaLine([
                    "\(totals.count) 个种子",
                    "↑ \(ActivityFormat.rate(Double(totals.upSpeed)))",
                    "已上传 \(ActivityFormat.bytes(Double(totals.uploaded)))",
                ]))
                .font(.footnote).monospacedDigit().foregroundStyle(Theme.textMuted).lineLimit(1)
            }
        }
        .padding(.vertical, 4)
        .accessibilityIdentifier("boost-section")
    }
}

// MARK: - 最近播放 / 观看统计 / 最近完成

/// 最近播放一行：小海报 · 片名 / 成员 · 设备 · 右侧上下两行「看到哪 / 相对时间」。
/// 观看进度放右列且不截断：设备名（Jellyfin 客户端常带长长的型号与系统版本）只截断它自己那一行，
/// 不会再把「看到 42%」挤没。
struct ActivityRecentPlayRow: View {
    let entry: API.PlaybackLogEntryView
    @Environment(\.api) private var api

    var body: some View {
        let playing = entry.endedAt == nil
        HStack(spacing: 12) {
            RemoteImage(url: api.image(entry.media.posterUrl, .posterCard), placeholderSymbol: "film")
                .frame(width: 34, height: 50)
                .clipShape(.rect(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.white.opacity(0.1)))
            VStack(alignment: .leading, spacing: 2) {
                title.font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                Text(WatchFormat.metaLine([entry.memberName, entry.deviceName.isEmpty ? entry.client : entry.deviceName]))
                    .font(.footnote).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
            Spacer(minLength: 8)
            // 结果在上（与片名同一行、字重高一档），时间在下（与副标题同一行、更淡）
            VStack(alignment: .trailing, spacing: 2) {
                if playing {
                    Text("播放中").fontWeight(.medium).foregroundStyle(Theme.success)
                } else if entry.completed {
                    Label("看完", systemImage: "checkmark").labelStyle(.titleAndIcon).fontWeight(.medium).foregroundStyle(Theme.success)
                } else if let percent = entry.progressPercent {
                    Text("看到 \(percent)%").fontWeight(.medium).foregroundStyle(Theme.textMuted)
                }
                if !playing {
                    Text(ActivityFormat.relative(entry.startedAt)).foregroundStyle(Theme.textFaint)
                }
            }
            .font(.footnote)
            .monospacedDigit()
            .fixedSize()
        }
    }

    private var title: Text {
        var text = Text(entry.media.title.isEmpty ? "（条目已删除）" : entry.media.title)
        if let unit = WatchFormat.unitLabel(entry.media) {
            text = text + Text(" \(unit)").fontWeight(.regular).foregroundStyle(Theme.textMuted)
        }
        return text
    }
}

/// 最近 7 天观看摘要：总时长 + 较前 7 天涨跌 + 按天柱状（今天高亮），点进观看统计
struct ActivityWeeklyWatchCard: View {
    let stats: API.PlaybackWatchStatsView

    private static let weekdays = ["日", "一", "二", "三", "四", "五", "六"]

    var body: some View {
        // 服务端按「含今天往前 N 天」给，可能多出一天；只画最近 7 根。x 用日期（星期几会重名），轴上再换成星期
        let bars = stats.byDay.suffix(7).map { (date: $0.date, hours: Double($0.watchedMs) / 3_600_000) }
        let today = bars.last?.date
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("最近 7 天").font(.footnote).foregroundStyle(Theme.textMuted)
                    total
                }
                Spacer()
                if let delta { Text(delta.text).font(.footnote.weight(.semibold)).foregroundStyle(delta.color) }
            }
            Chart(bars, id: \.date) { bar in
                BarMark(x: .value("日期", bar.date), y: .value("小时", bar.hours), width: .ratio(0.55))
                    .foregroundStyle(bar.date == today ? Theme.info : Theme.info.opacity(0.45))
                    .clipShape(.rect(cornerRadius: 4))
            }
            .chartYAxis(.hidden)
            .chartXAxis {
                AxisMarks { value in
                    AxisValueLabel {
                        if let date = value.as(String.self) { Text(Self.weekday(date)).foregroundStyle(Theme.textFaint) }
                    }
                }
            }
            .frame(height: 84)
        }
        .padding(.vertical, 8)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("weekly-watch")
    }

    /// 「14 小时 18 分」，数字大、单位小
    private var total: some View {
        let minutes = Int((Double(stats.current.watchedMs) / 60_000).rounded())
        let hours = minutes / 60
        return HStack(alignment: .firstTextBaseline, spacing: 3) {
            if hours > 0 {
                Text("\(hours)").font(.system(size: 30, weight: .bold, design: .rounded))
                Text("小时").font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            if minutes % 60 > 0 || hours == 0 {
                Text("\(minutes % 60)").font(.system(size: 30, weight: .bold, design: .rounded))
                Text("分钟").font(.subheadline).foregroundStyle(Theme.textMuted)
            }
        }
        .monospacedDigit()
    }

    private var delta: (text: String, color: Color)? {
        guard stats.previousAvailable else { return nil }
        let current = Double(stats.current.watchedMs), previous = Double(stats.previous.watchedMs)
        if previous <= 0 { return current > 0 ? ("比前 7 天新增", Theme.success) : nil }
        let ratio = Int(((current - previous) / previous * 100).rounded())
        if ratio == 0 { return ("与前 7 天持平", Theme.textFaint) }
        return ratio > 0 ? ("比前 7 天 ↑ \(ratio)%", Theme.success) : ("比前 7 天 ↓ \(-ratio)%", Theme.textMuted)
    }

    /// "2026-09-26" → 「六」
    private static func weekday(_ date: String) -> String {
        let parts = date.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3,
              let day = Calendar(identifier: .gregorian).date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2]))
        else { return date }
        return weekdays[Calendar(identifier: .gregorian).component(.weekday, from: day) - 1]
    }
}

/// 最近完成一行：结果图标 · 标题 / 摘要 · 右侧相对时间
struct ActivityFinishedJobRow: View {
    let job: API.JobView

    var body: some View {
        let dismissed = job.status == "failed" && TaskCenter.isDismissed(job)
        HStack(spacing: 12) {
            Image(systemName: job.status == "succeeded" ? "checkmark.circle.fill" : dismissed ? "eye.slash" : "xmark.circle")
                .font(.title3)
                .foregroundStyle(job.status == "succeeded" ? Theme.success : Theme.textFaint)
                .frame(width: 26)
            VStack(alignment: .leading, spacing: 2) {
                Text(TaskCenter.historicalJobTitle(job)).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                Text(TaskCenter.ingestHistoryDetail(job)?.summary ?? TaskCenter.historicalJobSummary(job))
                    .font(.footnote).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
            Spacer(minLength: 8)
            Text(ActivityFormat.relative(job.finishedAt ?? job.createdAt)).font(.footnote).foregroundStyle(Theme.textFaint).fixedSize()
        }
        .padding(.vertical, 2)
    }
}
