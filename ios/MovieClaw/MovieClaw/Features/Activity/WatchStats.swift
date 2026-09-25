import Charts
import SwiftUI

/// 「观看统计」切片（Web `watch-stats-panel.tsx`）。
///
/// 骨架照仪表盘通行做法：**指标卡 → TOP 3 → 主图 → 一组同构的分解面板 → 时段热力图**。
/// - 指标卡只有数字与较上一周期的变化；点哪张卡主图就切到哪个指标；
/// - 主图把本周期柱子与上一周期虚线画在同一坐标系（Swift Charts）；柱子摆不下（每根 < 6pt）按周折桶；
/// - 分解面板同一个模板（名称 · 值 · 占比条），默认五行，多出来的合成「其他 N 个」或「展开全部」；
///   成员行可点即钻取；
/// - 星期 × 小时热力图回答「家里什么时候有人在看」。
struct WatchStatsPanel: View {
    let scope: String
    let days: Int
    let memberId: Int?
    let memberLabel: String?
    let onMemberSelect: (Int?) -> Void
    let onDaysChange: (Int) -> Void
    let onShowAll: () -> Void

    @Environment(\.api) private var api
    @State private var stats: API.PlaybackWatchStatsView?
    @State private var error: String?
    @State private var metric: WatchMetric = .watched

    private struct Query: Hashable {
        var scope: String
        var days: Int
        var memberId: Int?
    }

    var body: some View {
        Group {
            if let error {
                ActivityWarningBanner(message: error)
            } else if let stats {
                content(stats)
            } else {
                HStack(spacing: 10) {
                    ProgressView()
                    Text("正在读取观看统计…").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 60)
            }
        }
        .task(id: Query(scope: scope, days: days, memberId: memberId)) {
            do {
                // tz_offset：本地时区相对 UTC 的分钟数（热力图与按天分组按本地时间算）
                let offset = TimeZone.current.secondsFromGMT() / 60
                stats = try await api.playbackStatsWatch(days: days, tzOffset: offset, memberId: memberId, scope: scope)
                error = nil
            } catch is CancellationError {
            } catch {
                self.error = error.localizedDescription.isEmpty ? "观看统计加载失败" : error.localizedDescription
            }
        }
    }

    @ViewBuilder
    private func content(_ stats: API.PlaybackWatchStatsView) -> some View {
        let who = memberId != nil ? (memberLabel ?? "这位成员") : nil
        if stats.current.plays == 0, !stats.previousAvailable {
            ActivityEmptyCard(
                systemImage: "waveform.path.ecg",
                title: who.map { "\($0)最近 \(days) 天没有播放" } ?? "最近 \(days) 天没有播放记录",
                message: "统计从播放日志来：从现在起每一场播放都会计入，看得越久这里越有得看。"
            ) { widenActions }
        } else {
            VStack(alignment: .leading, spacing: 16) {
                metricCards(stats)
                if stats.current.plays == 0 {
                    trendCard(stats)
                    ActivityEmptyCard(
                        systemImage: "waveform.path.ecg",
                        title: who.map { "\($0)本周期没有播放" } ?? "本周期没有播放",
                        message: "上一周期有 \(stats.previous.plays) 场；本周期一场都没有，没有可以分解的数据。"
                    ) { widenActions }
                } else {
                    FavoritePodium(
                        favorites: stats.favorites, previous: stats.previousFavorites, drilled: memberId != nil,
                        hiddenCount: stats.hiddenTitleCount, onShowAll: onShowAll
                    )
                    trendCard(stats)
                    breakdowns(stats)
                    HourHeatmap(matrix: stats.byHour)
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("watch-stats")
        }
    }

    /// 空状态里能做的事：清掉成员钻取、把周期拉到最长
    @ViewBuilder private var widenActions: some View {
        if memberId != nil { ActivityPillButton(title: "查看全部成员") { onMemberSelect(nil) } }
        if days < 90 { ActivityPillButton(title: "看最近 90 天") { onDaysChange(90) } }
    }

    private func metricCards(_ stats: API.PlaybackWatchStatsView) -> some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
            ForEach(WatchMetric.allCases, id: \.self) { item in
                MetricCard(metric: item, stats: stats, selected: item == metric) { metric = item }
            }
        }
    }

    private func trendCard(_ stats: API.PlaybackWatchStatsView) -> some View {
        TrendChart(stats: stats, metric: metric)
            .padding(12)
            .statsPanel()
    }

    @ViewBuilder
    private func breakdowns(_ stats: API.PlaybackWatchStatsView) -> some View {
        let total = Double(stats.current.watchedMs)
        BreakdownPanel(
            title: "按成员", note: memberId != nil ? "已钻取到一个成员，再点一次取消" : "点成员名钻取",
            total: total, unit: "个成员", formatValue: { ActivityFormat.watched(Int($0)) },
            rows: stats.byMember.map { row in
                BreakdownRow(
                    key: "\(row.memberId)", label: Text(row.memberName), value: Double(row.watchedMs),
                    valueLabel: ActivityFormat.watched(row.watchedMs), secondary: "\(row.plays) 场 · 看完 \(row.completed)",
                    selected: memberId == row.memberId,
                    onSelect: { onMemberSelect(memberId == row.memberId ? nil : row.memberId) }
                )
            }
        )
        BreakdownPanel(
            title: "按客户端", total: total, unit: "个客户端", formatValue: { ActivityFormat.watched(Int($0)) },
            rows: stats.byClient.map { row in
                BreakdownRow(key: row.client, label: Text(row.client), value: Double(row.watchedMs),
                             valueLabel: ActivityFormat.watched(row.watchedMs), secondary: "\(row.plays) 场")
            }
        )
        BreakdownPanel(
            title: "看得最多", total: total, unit: "部", formatValue: { ActivityFormat.watched(Int($0)) },
            fold: .expand,
            rows: stats.topTitles.enumerated().map { index, row in
                BreakdownRow(
                    key: "\(row.media.mediaItemId)-\(index)", label: Text(row.media.title.isEmpty ? "（条目已删除）" : row.media.title),
                    value: Double(row.watchedMs), valueLabel: ActivityFormat.watched(row.watchedMs), secondary: "\(row.plays) 场",
                    media: row.media
                )
            },
            emptyText: stats.hiddenTitleCount > 0 ? nil : "播放过的条目已被删除"
        ) {
            HiddenCountRow(count: stats.hiddenTitleCount, noun: "部作品", onShowAll: onShowAll)
        }
        let tierTotal = Double(stats.byTier.reduce(0) { $0 + $1.plays })
        BreakdownPanel(
            title: "按播放方式", note: "仅网页播放；Jellyfin 客户端恒为直连",
            total: tierTotal, unit: "种", formatValue: { "\(Int($0)) 场" },
            rows: stats.byTier.map { row in
                BreakdownRow(key: "\(row.tier)", label: Text(row.label), value: Double(row.plays), valueLabel: "\(row.plays) 场")
            },
            emptyText: "本周期没有网页播放；Jellyfin 客户端不经过转码，不在这里分解"
        )
    }
}

// MARK: - 指标

/// 四个指标的定义（取值、汇总展示、坐标轴、变化口径）
enum WatchMetric: CaseIterable {
    case watched, plays, completion, members

    var label: String {
        switch self {
        case .watched: "观看时长"
        case .plays: "播放场次"
        case .completion: "看完率"
        case .members: "活跃成员"
        }
    }

    /// 一段连续日期的值（传一天即当天，传一周即当周；活跃成员按周折桶时取日均）
    func of(days rows: [API.PlaybackStatsDayRow]) -> Double {
        switch self {
        case .watched: Double(rows.reduce(0) { $0 + $1.watchedMs })
        case .plays: Double(rows.reduce(0) { $0 + $1.plays })
        case .completion:
            rows.reduce(0) { $0 + $1.plays } > 0
                ? Double(rows.reduce(0) { $0 + $1.completed }) / Double(rows.reduce(0) { $0 + $1.plays }) : 0
        case .members: rows.isEmpty ? 0 : Double(rows.reduce(0) { $0 + $1.members }) / Double(rows.count)
        }
    }

    func of(totals t: API.PlaybackStatsTotals) -> Double {
        switch self {
        case .watched: Double(t.watchedMs)
        case .plays: Double(t.plays)
        case .completion: t.plays > 0 ? Double(t.completed) / Double(t.plays) : 0
        case .members: Double(t.activeMembers)
        }
    }

    private static func hours(_ ms: Double) -> String {
        String(format: ms >= 360_000_000 ? "%.0f" : "%.1f", ms / 3_600_000)
    }

    func format(_ v: Double) -> String {
        switch self {
        case .watched: v >= 3_600_000 ? "\(Self.hours(v)) 小时" : ActivityFormat.watched(Int(v))
        case .plays: "\(Int(v)) 场"
        case .completion: "\(Int((v * 100).rounded()))%"
        case .members: "\(Self.trim((v * 10).rounded() / 10)) 人"
        }
    }

    /// 指标卡大数字：[数值, 单位]
    func parts(_ v: Double) -> (String, String) {
        switch self {
        case .watched:
            v >= 3_600_000 ? (Self.hours(v), "小时") : ("\(max(v > 0 ? 1 : 0, Int((v / 60_000).rounded())))", "分钟")
        case .plays: ("\(Int(v))", "场")
        case .completion: ("\(Int((v * 100).rounded()))", "%")
        case .members: (Self.trim((v * 10).rounded() / 10), "人")
        }
    }

    func axis(_ v: Double) -> String {
        switch self {
        case .watched: String(format: v >= 36_000_000 ? "%.0fh" : "%.1fh", v / 3_600_000)
        case .plays, .members: "\(Int(v.rounded()))"
        case .completion: "\(Int((v * 100).rounded()))%"
        }
    }

    var deltaInPoints: Bool { self == .completion }
    var bucketNote: String? { self == .members ? "日均" : nil }

    private static func trim(_ v: Double) -> String {
        v == v.rounded() ? "\(Int(v))" : String(format: "%.1f", v)
    }
}

private struct MetricCard: View {
    let metric: WatchMetric
    let stats: API.PlaybackWatchStatsView
    let selected: Bool
    let onSelect: () -> Void

    var body: some View {
        let current = metric.of(totals: stats.current)
        let previous = metric.of(totals: stats.previous)
        let (value, unit) = metric.parts(current)
        Button(action: onSelect) {
            VStack(alignment: .leading, spacing: 8) {
                Text(metric.label).font(.caption.weight(.medium)).foregroundStyle(Theme.textMuted)
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    Text(value).font(.system(size: 26, weight: .bold)).monospacedDigit().foregroundStyle(Theme.text)
                    Text(unit).font(.footnote.weight(.medium)).foregroundStyle(Theme.textFaint)
                }
                .lineLimit(1)
                .minimumScaleFactor(0.7)
                HStack(spacing: 6) {
                    DeltaChip(current: current, previous: previous, available: stats.previousAvailable, inPoints: metric.deltaInPoints)
                    Text(stats.previousAvailable ? "上期 \(metric.format(previous))" : "暂无上一周期数据")
                        .font(.system(size: 11)).monospacedDigit().foregroundStyle(Theme.textFaint).lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 13)
            .background(selected ? Theme.info.opacity(0.07) : Color.white.opacity(0.03), in: .rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(selected ? Theme.info.opacity(0.6) : Color.white.opacity(0.08)))
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("metric-\(metric.label)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// 较上一周期的变化：涨绿、跌红、持平灰；没有上期就不出
private struct DeltaChip: View {
    let current: Double
    let previous: Double
    let available: Bool
    var inPoints = false

    var body: some View {
        if let (text, direction) = model {
            let color: Color = direction > 0 ? Theme.success : direction < 0 ? Theme.danger : Theme.textMuted
            HStack(spacing: 2) {
                if direction != 0 { Text(direction > 0 ? "▲" : "▼").font(.system(size: 8)) }
                Text(text)
            }
            .font(.system(size: 11, weight: .semibold))
            .monospacedDigit()
            .foregroundStyle(color)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background((direction == 0 ? Color.white.opacity(0.08) : color.opacity(0.15)), in: .rect(cornerRadius: 6))
            .accessibilityLabel("较上一周期 \(text)")
        }
    }

    private var model: (String, Int)? {
        guard available else { return nil }
        if inPoints {
            let points = Int(((current - previous) * 100).rounded())
            return ("\(abs(points)) 个百分点", points.signum())
        }
        if previous <= 0 { return current <= 0 ? nil : ("新增", 1) }
        let ratio = (current - previous) / previous
        let direction = ratio > 0 ? 1 : ratio < 0 ? -1 : 0
        return (direction == 0 ? "持平" : "\(Int((abs(ratio) * 100).rounded()))%", direction)
    }
}

// MARK: - 主图

/// 本周期柱子 + 上一周期虚线，同一坐标系；点按柱子看当天（或当周）读数
private struct TrendChart: View {
    let stats: API.PlaybackWatchStatsView
    let metric: WatchMetric
    @State private var selected: Int?
    @State private var width: CGFloat = 0

    private struct Point: Identifiable {
        var index: Int
        var current: Double
        var previous: Double?
        var label: String
        var previousLabel: String?
        var dayCount: Int
        var id: Int { index }
    }

    /// 从末尾往前每 size 天一桶：最新一桶一定是满的
    private static func bucketize(_ rows: [API.PlaybackStatsDayRow], size: Int) -> [[API.PlaybackStatsDayRow]] {
        var out: [[API.PlaybackStatsDayRow]] = []
        var end = rows.count
        while end > 0 {
            out.insert(Array(rows[max(0, end - size) ..< end]), at: 0)
            end -= size
        }
        return out
    }

    private static func dayLabel(_ date: String) -> String {
        let parts = date.split(separator: "-")
        guard parts.count == 3, let m = Int(parts[1]), let d = Int(parts[2]) else { return date }
        return "\(m)月\(d)日"
    }

    private static func bucketLabel(_ rows: [API.PlaybackStatsDayRow]) -> String {
        guard let first = rows.first, let last = rows.last else { return "" }
        return rows.count == 1 ? dayLabel(first.date) : "\(dayLabel(first.date)) – \(dayLabel(last.date))"
    }

    private var weekly: Bool {
        // 可用宽度（扣掉 y 轴约 40pt）除以天数 < 6 时按周折桶（90 天在手机上每根不到 4pt）
        !stats.byDay.isEmpty && max(0, width - 40) / CGFloat(stats.byDay.count) < 6
    }

    private var points: [Point] {
        let size = weekly ? 7 : 1
        let buckets = Self.bucketize(stats.byDay, size: size)
        let previous = Self.bucketize(stats.previousByDay, size: size)
        let showPrevious = stats.previousAvailable && previous.count == buckets.count
        return buckets.enumerated().map { index, rows in
            Point(
                index: index, current: metric.of(days: rows),
                previous: showPrevious ? metric.of(days: previous[index]) : nil,
                label: Self.bucketLabel(rows), previousLabel: showPrevious ? Self.bucketLabel(previous[index]) : nil,
                dayCount: rows.count
            )
        }
    }

    var body: some View {
        let points = points
        let showPrevious = points.first?.previous != nil
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(metric.label).fontWeight(.semibold).foregroundStyle(Theme.textMuted)
                Text(weekly ? "按周" : "按天").foregroundStyle(Theme.textFaint)
            }
            .font(.caption)
            chart(points, showPrevious: showPrevious)
                .frame(height: 200)
                .overlay(alignment: .top) {
                    if points.allSatisfy({ $0.current <= 0 }) {
                        Text("本周期没有播放\(showPrevious ? "，虚线是上一周期" : "")")
                            .font(.caption).foregroundStyle(Theme.textMuted)
                            .padding(.horizontal, 12).padding(.vertical, 4)
                            .background(Theme.background.opacity(0.85), in: .capsule)
                            .padding(.top, 30)
                    }
                }
            if let selected, points.indices.contains(selected) {
                tooltip(points[selected])
            }
            HStack(spacing: 16) {
                HStack(spacing: 6) {
                    RoundedRectangle(cornerRadius: 3).fill(Theme.info).frame(width: 10, height: 10)
                    Text("本周期")
                }
                if showPrevious {
                    HStack(spacing: 6) {
                        Rectangle().stroke(Color.white.opacity(0.32), style: StrokeStyle(lineWidth: 2, dash: [3, 3])).frame(width: 16, height: 0.5)
                        Text("上一周期")
                    }
                }
            }
            .font(.caption)
            .foregroundStyle(Theme.textFaint)
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
        .onChange(of: metric) { selected = nil }
    }

    private func chart(_ points: [Point], showPrevious: Bool) -> some View {
        let maxValue = points.reduce(0.0) { max($0, $1.current, $1.previous ?? 0) }
        let yMax = Self.niceMax(maxValue)
        let slot = max(0, width - 48) / CGFloat(max(points.count, 1))
        let barWidth = max(1, slot - min(6, max(1, slot * 0.3)))
        let tickEvery = max(1, Int((Double(points.count) / Double(max(2, min(5, Int(width / 70))))).rounded()))
        return Chart {
            ForEach(points) { point in
                // x 是数值轴（桶序号），柱宽按槽位宽度算定值（.ratio 在数值轴上会退化成 0 宽）
                BarMark(x: .value("日期", point.index), y: .value(metric.label, point.current), width: .fixed(barWidth))
                    .foregroundStyle(Theme.info.opacity(selected == nil || selected == point.index ? 0.95 : 0.55))
                    .cornerRadius(min(3, barWidth / 2))
            }
            if showPrevious {
                ForEach(points) { point in
                    LineMark(x: .value("日期", point.index), y: .value("上一周期", point.previous ?? 0), series: .value("系列", "上一周期"))
                        .foregroundStyle(Color.white.opacity(0.32))
                        .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 4]))
                }
            }
            if let selected {
                RuleMark(x: .value("日期", selected)).foregroundStyle(Color.white.opacity(0.08)).lineStyle(StrokeStyle(lineWidth: 14))
            }
        }
        .chartYScale(domain: 0 ... yMax)
        .chartXScale(domain: -0.5 ... Double(max(points.count, 1)) - 0.5)
        .chartYAxis {
            AxisMarks(position: .leading, values: Array(stride(from: 0, through: yMax, by: yMax / 4))) { value in
                AxisGridLine().foregroundStyle(Color.white.opacity(0.07))
                AxisValueLabel {
                    if let v = value.as(Double.self) { Text(metric.axis(v)).font(.system(size: 10)).foregroundStyle(Theme.textFaint) }
                }
            }
        }
        .chartXAxis {
            AxisMarks(values: points.map(\.index).filter { $0 % tickEvery == 0 || $0 == points.count - 1 }) { value in
                AxisValueLabel {
                    if let i = value.as(Int.self), points.indices.contains(i) {
                        Text(Self.dayLabel(dateOf(i))).font(.system(size: 10)).foregroundStyle(Theme.textFaint)
                    }
                }
            }
        }
        .chartXSelection(value: Binding(
            get: { selected },
            set: { selected = $0.map { min(max(0, $0), points.count - 1) } }
        ))
        .accessibilityLabel("\(metric.label)走势")
    }

    private func dateOf(_ index: Int) -> String {
        let size = weekly ? 7 : 1
        let buckets = Self.bucketize(stats.byDay, size: size)
        return buckets.indices.contains(index) ? (buckets[index].first?.date ?? "") : ""
    }

    private func tooltip(_ point: Point) -> some View {
        let note = weekly ? (metric.bucketNote.map { "\($0) " } ?? "") : ""
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 4) {
                Text(point.label).fontWeight(.semibold).foregroundStyle(Theme.text.opacity(0.85))
                if weekly, point.dayCount < 7 { Text("\(point.dayCount) 天").foregroundStyle(Theme.textFaint) }
            }
            HStack(spacing: 6) {
                RoundedRectangle(cornerRadius: 2).fill(Theme.info).frame(width: 8, height: 8)
                Text("本周期 \(note)\(metric.format(point.current))").foregroundStyle(Theme.text.opacity(0.8))
            }
            if let previous = point.previous {
                HStack(spacing: 6) {
                    Rectangle().stroke(Color.white.opacity(0.32), style: StrokeStyle(lineWidth: 1, dash: [2, 2])).frame(width: 8, height: 0.5)
                    Text("上一周期 \(note)\(metric.format(previous))").foregroundStyle(Theme.textMuted)
                    if let label = point.previousLabel { Text(label).foregroundStyle(Theme.textFaint) }
                }
            }
        }
        .font(.caption)
        .monospacedDigit()
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.white.opacity(0.06), in: .rect(cornerRadius: 10))
    }

    /// 坐标轴的整齐上限：1 / 2 / 5 × 10^n 里第一个不小于最大值的
    static func niceMax(_ value: Double) -> Double {
        guard value > 0 else { return 1 }
        let base = pow(10, floor(log10(value)))
        for step in [1.0, 2, 5, 10] where value <= step * base { return step * base }
        return 10 * base
    }
}

// MARK: - 分解面板

struct BreakdownRow: Identifiable {
    var key: String
    var label: Text
    var value: Double
    var valueLabel: String
    var secondary: String?
    var selected = false
    var muted = false
    var onSelect: (() -> Void)?
    /// 作品行：左侧小海报，名称可跳详情
    var media: API.MediaActivityTarget?
    var id: String { key }
}

/// 分解面板（名称 · 值 · 占比条）。条的长度就是份额，与右边百分比、总量三者自洽。
/// 行数封顶：`other` 把第 6 行起合成「其他 N 个」；`expand` 留「展开全部」（作品不适合合并）。
private struct BreakdownPanel<Footer: View>: View {
    enum Fold { case other, expand }

    let title: String
    var note: String?
    let total: Double
    let unit: String
    let formatValue: (Double) -> String
    var fold: Fold = .other
    var limit = 5
    let rows: [BreakdownRow]
    var emptyText: String? = "本周期没有数据"
    @ViewBuilder var footer: () -> Footer

    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(title).font(.caption.weight(.semibold)).foregroundStyle(Theme.textMuted)
                if let note { Text(note).font(.system(size: 11)).foregroundStyle(Theme.textFaint) }
            }
            VStack(spacing: 0) {
                if rows.isEmpty, let emptyText {
                    Text(emptyText).font(.caption).foregroundStyle(Theme.textFaint)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(14)
                }
                ForEach(Array(visibleRows.enumerated()), id: \.element.id) { index, row in
                    if index > 0 { Divider().overlay(Color.white.opacity(0.06)) }
                    BreakdownRowView(row: row, total: total)
                }
                if fold == .expand, rows.count > limit {
                    Divider().overlay(Color.white.opacity(0.06))
                    Button(expanded ? "收起" : "展开全部 \(rows.count) \(unit)") { expanded.toggle() }
                        .font(.caption.weight(.medium))
                        .foregroundStyle(Theme.info)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .buttonStyle(.plain)
                }
                footer()
            }
            .statsPanel()
        }
    }

    private var visibleRows: [BreakdownRow] {
        guard rows.count > limit else { return rows }
        if fold == .expand { return expanded ? rows : Array(rows.prefix(limit)) }
        let rest = rows.dropFirst(limit)
        let value = rest.reduce(0) { $0 + $1.value }
        return Array(rows.prefix(limit)) + [BreakdownRow(
            key: "__other", label: Text("其他 \(rest.count) \(unit)"), value: value, valueLabel: formatValue(value), muted: true
        )]
    }
}

extension BreakdownPanel where Footer == EmptyView {
    init(title: String, note: String? = nil, total: Double, unit: String, formatValue: @escaping (Double) -> String,
         fold: Fold = .other, rows: [BreakdownRow], emptyText: String? = "本周期没有数据") {
        self.init(title: title, note: note, total: total, unit: unit, formatValue: formatValue, fold: fold,
                  rows: rows, emptyText: emptyText) { EmptyView() }
    }
}

private struct BreakdownRowView: View {
    let row: BreakdownRow
    let total: Double
    @Environment(\.api) private var api

    var body: some View {
        let share = total > 0 ? Int((row.value / total * 100).rounded()) : 0
        let content = HStack(spacing: 12) {
            if let media = row.media {
                ActivityPoster(media: media, width: 24, height: 36, radius: 5)
            }
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if let media = row.media {
                        ActivityTitleText(media: media, showYear: false).layoutPriority(1)
                    } else {
                        row.label.font(.subheadline.weight(.medium))
                            .foregroundStyle(row.muted ? Theme.textFaint : Theme.text.opacity(0.85)).lineLimit(1)
                    }
                    if let secondary = row.secondary {
                        Text(secondary).font(.system(size: 11)).monospacedDigit().foregroundStyle(Theme.textFaint).lineLimit(1).fixedSize()
                    }
                    Spacer(minLength: 4)
                    HStack(spacing: 5) {
                        Text(row.valueLabel).foregroundStyle(Theme.text.opacity(0.7))
                        Text("\(share)%").foregroundStyle(Theme.textFaint)
                    }
                    .font(.caption).monospacedDigit().fixedSize()
                }
                GeometryReader { proxy in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.white.opacity(0.06))
                        Capsule()
                            .fill(row.selected ? Theme.success : row.muted ? Color.white.opacity(0.22) : Theme.info.opacity(0.9))
                            .frame(width: proxy.size.width * CGFloat(max(1, share)) / 100)
                    }
                }
                .frame(height: 3)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)

        if let onSelect = row.onSelect {
            Button(action: onSelect) { content.contentShape(.rect) }
                .buttonStyle(.plain)
                .background(row.selected ? Color.white.opacity(0.03) : .clear)
                .accessibilityAddTraits(row.selected ? .isSelected : [])
        } else {
            content
        }
    }
}

// MARK: - TOP 3

/// 本期最受欢迎前三（按看过的人数、并列看时长）；钻取到单个成员时退成「本期看得最多」。
/// 背景是第一名海报放大模糊后的环境光，整块随作品换色。
private struct FavoritePodium: View {
    let favorites: [API.PlaybackStatsTitleRow]
    let previous: [API.PlaybackStatsTitleRow]
    let drilled: Bool
    let hiddenCount: Int
    let onShowAll: () -> Void

    @Environment(\.api) private var api

    var body: some View {
        if favorites.isEmpty {
            ActivityEmptyCard(
                systemImage: hiddenCount > 0 ? "lock" : "star",
                title: hiddenCount > 0 ? "本期最受欢迎的作品都在你的浏览范围外" : "本期还没有可以上榜的作品",
                message: hiddenCount > 0 ? "\(hiddenCount) 部作品来自你设为不可见的库" : "播放过的条目已被删除，不再进榜",
                compact: true
            ) {
                if hiddenCount > 0 { ActivityPillButton(title: "显示全部", action: onShowAll) }
            }
            .statsPanel()
        } else {
            VStack(alignment: .leading, spacing: 18) {
                HStack(spacing: 8) {
                    Text("TOP 3")
                        .font(.system(size: 10, weight: .black))
                        .tracking(1.2)
                        .foregroundStyle(Theme.background)
                        .padding(.horizontal, 6).padding(.vertical, 3)
                        .background(Theme.info, in: .rect(cornerRadius: 3))
                    Text(drilled ? "本期看得最多" : "本期最受欢迎").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.9))
                    Text(drilled ? "按观看时长" : "按看过的人数，并列看时长").font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                }
                ForEach(Array(favorites.prefix(3).enumerated()), id: \.offset) { rank, row in
                    TopEntry(row: row, rank: rank, drilled: drilled, previous: previous)
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background {
                ZStack {
                    RemoteImage(url: api.image(favorites[0].media.posterUrl, .posterCard))
                        .scaleEffect(1.5)
                        .blur(radius: 40)
                        .saturation(1.5)
                        .opacity(0.45)
                    LinearGradient(
                        colors: [Color(red: 11 / 255, green: 13 / 255, blue: 19 / 255).opacity(0.88), Color(red: 11 / 255, green: 13 / 255, blue: 19 / 255).opacity(0.75)],
                        startPoint: .leading, endPoint: .trailing
                    )
                }
                .allowsHitTesting(false)
            }
            .clipShape(.rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.08)))
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("favorite-podium")
        }
    }
}

/// 一个名次：巨大的描边数字压在海报左后方（Netflix TOP 10 语法），第一名更大
private struct TopEntry: View {
    let row: API.PlaybackStatsTitleRow
    let rank: Int
    let drilled: Bool
    let previous: [API.PlaybackStatsTitleRow]

    var body: some View {
        let hero = rank == 0
        HStack(alignment: .bottom, spacing: 0) {
            // 数字本身就是装饰：Web 用描边字，这里用自上而下渐隐的白（SwiftUI 没有文字描边）
            Text("\(rank + 1)")
                .font(.system(size: hero ? 112 : 84, weight: .black))
                .foregroundStyle(LinearGradient(colors: [Color.white.opacity(0.55), Color.white.opacity(0.06)], startPoint: .top, endPoint: .bottom))
                .frame(width: hero ? 64 : 48, alignment: .leading)
                .offset(y: hero ? 14 : 10)
            ActivityPoster(media: row.media, width: hero ? 110 : 76, height: hero ? 165 : 114, radius: 8)
                .shadow(color: .black.opacity(0.6), radius: 18, y: 12)
            VStack(alignment: .leading, spacing: 4) {
                Text(["冠军", "亚军", "季军"][rank]).font(.system(size: 10, weight: .bold)).tracking(2).foregroundStyle(Theme.textFaint)
                ActivityTitleText(media: row.media, episode: false, large: hero, showYear: false)
                let facts = [row.media.year.map { String($0) }, row.media.kind == "movie" ? "电影" : row.media.kind == "tv" ? "剧集" : nil]
                    .compactMap { $0 }.joined(separator: " · ")
                if !facts.isEmpty { Text(facts).font(.caption).foregroundStyle(Theme.textFaint) }
                Text([drilled ? nil : "\(row.members) 人看过", "\(row.plays) 场"].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(Theme.textMuted)
                Text(ActivityFormat.watched(row.watchedMs)).font(.caption).foregroundStyle(Theme.textMuted)
                if let change = rankChange {
                    Text(change.text)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(change.color)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(change.color.opacity(0.15), in: .rect(cornerRadius: 6))
                        .padding(.top, 4)
                }
            }
            .monospacedDigit()
            .padding(.leading, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(maxHeight: .infinity, alignment: .center)
        }
        .fixedSize(horizontal: false, vertical: true)
    }

    /// 与上一周期前三对照：同名次「蝉联」、换了名次「上期第 n」、上期不在榜「新上榜」
    private var rankChange: (text: String, color: Color)? {
        guard !previous.isEmpty else { return nil }
        guard let was = previous.firstIndex(where: { $0.media.mediaItemId == row.media.mediaItemId }) else {
            return ("新上榜", Theme.info)
        }
        if was == rank { return ("蝉联", Theme.success) }
        return ("\(was > rank ? "▲" : "▼") 上期第 \(was + 1)", was > rank ? Theme.success : Theme.textMuted)
    }
}

// MARK: - 时段热力图

/// 星期 × 小时的观看时长，越深越多；没数据也把 7×24 的格子画出来
private struct HourHeatmap: View {
    let matrix: [[Int]]
    private static let weekdays = ["一", "二", "三", "四", "五", "六", "日"]

    var body: some View {
        let flat = matrix.flatMap { $0 }
        let maxValue = max(1e-9, Double(flat.max() ?? 0))
        let total = flat.reduce(0, +)
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("观看时段").font(.caption.weight(.semibold)).foregroundStyle(Theme.textMuted)
                Text("星期 × 小时的观看时长，越深越多").font(.system(size: 11)).foregroundStyle(Theme.textFaint)
            }
            VStack(alignment: .leading, spacing: 2) {
                if total == 0 {
                    Text("本周期还没有累计到观看时长；有人看过之后这里会显示星期 × 小时的分布")
                        .font(.caption).foregroundStyle(Theme.textFaint).padding(.bottom, 6)
                }
                HStack(spacing: 2) {
                    Color.clear.frame(width: 18, height: 12)
                    ForEach(0 ..< 24, id: \.self) { hour in
                        Text(hour % 6 == 0 ? "\(hour)" : "")
                            .font(.system(size: 9)).monospacedDigit().foregroundStyle(Theme.textFaint)
                            .frame(maxWidth: .infinity)
                            .fixedSize(horizontal: true, vertical: false)
                            .frame(maxWidth: .infinity)
                    }
                }
                ForEach(Array(matrix.enumerated()), id: \.offset) { day, row in
                    HStack(spacing: 2) {
                        Text(Self.weekdays[safe: day] ?? "").font(.system(size: 10)).foregroundStyle(Theme.textFaint).frame(width: 18, alignment: .leading)
                        ForEach(Array(row.enumerated()), id: \.offset) { _, value in
                            RoundedRectangle(cornerRadius: 3)
                                .fill(value > 0
                                    ? Color(red: 127 / 255, green: 176 / 255, blue: 1).opacity(0.18 + Double(value) / maxValue * 0.82)
                                    : Color.white.opacity(0.04))
                                .frame(height: 14)
                                .frame(maxWidth: .infinity)
                        }
                    }
                }
            }
            .padding(12)
            .statsPanel()
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("观看时段热力图")
            .accessibilityIdentifier("watch-heatmap")
        }
    }
}

private extension Array {
    subscript(safe index: Int) -> Element? { indices.contains(index) ? self[index] : nil }
}

private extension View {
    func statsPanel() -> some View {
        background(Color.white.opacity(0.02), in: .rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.08)))
    }
}
