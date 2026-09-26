import SwiftUI

// 订阅首页（流媒体式版式）的纯展示逻辑：把订阅清单、整周预告、刚刚入库与下载快照
// 压成页面四块要说的话——Hero「下一部到手的」、「刚刚入库」、「日程」、剧集 / 电影两排海报。
//
// 版式按时间与意图拆，而不是按数据类型拆（设计讨论见 2026-09-26 的订阅页改版）：
//   1. 什么刚到、现在就能看（Hero 的「刚刚入库」、刚刚入库行）——订阅的回报时刻；
//   2. 下一个什么时候到（Hero 的倒计时、日程）——期待感；
//   3. 我在追哪些（剧集 / 电影海报行）——清单本身，最不急，放在最后。
// 视图层只负责排版；判定口径全部收在这里，便于单测（MovieClawTests/SubscriptionsHomeModelTests）。

// MARK: - 语气（状态色）

/// 订阅首页的状态语气。颜色只落在小圆点与少量文字上，底子一律是中性的玻璃与暗色：
/// 一屏里亮起来的只有「此刻真有进展」的那几处（同 Web 今日时间轨道「按事情有没有真的发生升级」的配色思路）。
enum SubsHomeTone: Equatable {
    /// 下载中：蓝，呼吸
    case live
    /// 整理中 / 刚刚入库 / 新一集：绿
    case ok
    /// 今天更新：淡紫
    case today
    /// 等待资源 / 找资源中：琥珀
    case warn
    /// 洗版中：青
    case upgrade
    /// 明天 / 未上映 / 已暂停 / 预计入库：中性
    case calm

    var color: Color {
        switch self {
        case .live: SubsColor.info
        case .ok: SubsColor.ok
        case .today: Color(red: 0.87, green: 0.84, blue: 1)
        case .warn: SubsColor.warn
        case .upgrade: SubsColor.upgrade
        case .calm: Color.white.opacity(0.62)
        }
    }

    /// 呼吸 / 发光只给「正在发生」的两档
    var glows: Bool { self == .live || self == .ok }
}

/// 海报左上角的状态小签：一张海报最多一个，讲它此刻最要紧的一件事
struct SubsHomeChip: Equatable {
    var text: String
    var tone: SubsHomeTone
    var pulse = false
}

// MARK: - Hero

/// Hero 的一张：一部作品「下一次能看到新东西」的那件事。
///
/// 两种讲法：讲时间的（下载中 / 整理中 / 今天 / 即将）用「小字说明 + 大号细体时刻」，
/// 不讲时间的（刚刚入库 / 追踪中）用「主说明 + 补充」。刚刚入库的主按钮是播放。
struct SubsHomeHeroSlide: Identifiable, Equatable {
    enum Stage {
        case downloading, organizing, arrived, today, upcoming, resting
    }

    var subscriptionId: Int
    var media: API.MediaBrief
    var stage: Stage
    var eyebrow: SubsHomeChip
    /// 大号时刻上方的小字（「S03E05 · 预计可看」）；nil = 这一张不讲时间
    var clockLabel: String?
    /// 大号细体时刻 / 词（「22:40」「周四」「即将可看」）
    var clock: String?
    /// 不讲时间时的主说明（「S04E01 · 命运之爱」）
    var detail: String?
    /// 补充说明（「2 小时前入库 · 共 3 集」）
    var footnote: String?
    /// 下载进度 0...1（只有下载中有）
    var progress: Double?
    /// 有值 = 主按钮是「播放」
    var play: PlayRequest?

    var id: Int { subscriptionId }
}

// MARK: - 日程

/// 日期条上的一天。站点日历（Asia/Shanghai）口径：日期与「今天」都由后端 expected_day / days_ahead 给出，
/// 客户端不复制时区规则（同 Web 首页预告）。
struct SubsHomeScheduleDay: Identifiable, Equatable {
    var daysAhead: Int
    /// 「今天」「周日」
    var weekday: String
    /// 「27」
    var dayNumber: String
    /// 「9月27日」（读屏与日程说明用）
    var dateLabel: String
    var entries: [SubsHomeScheduleEntry]
    var id: Int { daysAhead }
}

/// 日程里的一行：一部作品当天的更新（同一部剧当天多集合成一行）
struct SubsHomeScheduleEntry: Identifiable, Equatable {
    var subscriptionId: Int
    var title: String
    /// 「S03E05」「S02E01–E02」「电影」
    var episodeLabel: String
    /// 左列时刻「22:40」；给不出可信时间时为 nil（显示「待定」）
    var time: String?
    /// 「下载中 62%」「整理中」「预计入库」「等待资源」
    var status: String
    var tone: SubsHomeTone
    var progress: Double?
    /// 画面取订阅条目的剧照 / 海报（预告接口本身不带图）
    var media: API.MediaBrief?
    var id: Int { subscriptionId }
}

// MARK: - 海报行

/// 剧集 / 电影海报行的一张
struct SubsHomeShelfItem: Identifiable, Equatable {
    var sub: API.SubscriptionView
    var chip: SubsHomeChip?
    /// 海报下第二行：「第 3 季 · 4 / 8」「已收齐 · 全 5 季」「2026」
    var meta: String
    /// 海报底部的收录细线（剧集当季已收 / 应有）；nil = 不画
    var progress: Double?
    /// 已收齐 / 已暂停：压暗排在分隔线后面
    var resting: Bool
    var id: Int { sub.id }
}

/// 一排海报：在追的在前，歇着的（已收齐 / 已暂停）压暗排在分隔线后
struct SubsHomeShelf: Equatable {
    var active: [SubsHomeShelfItem]
    var resting: [SubsHomeShelfItem]
    /// 分隔线上的竖排小字：已收齐 / 已暂停 / 暂停·收齐
    var restingLabel: String

    var isEmpty: Bool { active.isEmpty && resting.isEmpty }
    var all: [SubsHomeShelfItem] { active + resting }
}

// MARK: - 组装

enum SubscriptionsHome {
    /// Hero 最多几张：再多用户也记不住，轮一圈要四十秒
    static let maxHeroSlides = 5
    /// 「刚刚入库」进 Hero 抢前排的时限：更早的仍在刚刚入库行里，Hero 只排在今天的预告之后
    static let freshArrivalWindow: TimeInterval = 48 * 3600

    /// 一部作品某一天的预告（同一部剧同一天多集合成一组）
    struct ArrivalGroup: Equatable {
        var subscriptionId: Int
        var title: String
        var kind: String
        var daysAhead: Int
        var expectedDay: String
        var units: [Int: [Int]]
        /// 整组以完成最慢的一集为准（同 Web 今日时间轨道的聚合口径）
        var presentation: TodayArrivalPresentation
        /// 组内下载中单元的平均进度 0...1
        var progress: Double?
        /// 整理中：下载完成时刻 + 本订阅历史「下载完成 → 入库」中位耗时，即预计能看的时刻（组内取最晚）
        var readyAt: Date?

        var episodeLabel: String {
            if kind == "movie" { return "电影" }
            return units.keys.sorted()
                .map { "S\(SubsFormat.pad($0))\(TodayArrivals.episodeRanges(units[$0] ?? []))" }
                .joined(separator: " · ")
        }
    }

    /// 预告行 → 按（订阅, 日期）分组，并套上下载器的实时进度 / ETA
    static func arrivalGroups(
        _ arrivals: [API.TodayArrivalView],
        tasks: [API.DownloadTaskView],
        now: Date
    ) -> [ArrivalGroup] {
        let taskByHash = Dictionary(tasks.map { ($0.infoHash.lowercased(), $0) }, uniquingKeysWith: { first, _ in first })
        struct Key: Hashable { var subscriptionId: Int; var daysAhead: Int }
        var order: [Key] = []
        var rows: [Key: [(API.TodayArrivalView, TodayArrivalPresentation, API.DownloadTaskView?)]] = [:]
        for arrival in arrivals {
            let task = arrival.infoHash.flatMap { taskByHash[$0.lowercased()] }
            let key = Key(subscriptionId: arrival.subscriptionId, daysAhead: arrival.daysAhead)
            if rows[key] == nil { order.append(key) }
            rows[key, default: []].append((arrival, TodayArrivals.presentation(arrival, task: task, now: now), task))
        }
        return order.compactMap { key in
            guard let group = rows[key], let first = group.first else { return nil }
            var units: [Int: [Int]] = [:]
            for row in group { units[row.0.seasonNumber, default: []].append(row.0.episodeNumber) }
            let blocking = group.map(\.1).sorted { left, right in
                if left.stageOrder != right.stageOrder { return left.stageOrder < right.stageOrder }
                switch (left.estimatedAt, right.estimatedAt) {
                case (nil, nil): return false
                case (nil, _): return true
                case (_, nil): return false
                case let (l?, r?): return l > r
                }
            }.first ?? first.1
            let downloading = group.compactMap { row -> Double? in
                guard row.0.status == "grabbed", let progress = row.2?.progress else { return nil }
                return min(max(progress, 0), 1)
            }
            let ready = group.compactMap { row -> Date? in
                guard row.0.status == "downloaded", let done = Formatters.date(row.0.downloadedAt) else { return nil }
                return done.addingTimeInterval(TimeInterval(row.0.estimatedDownloadToImportMinutes * 60))
            }
            return ArrivalGroup(
                subscriptionId: key.subscriptionId,
                title: first.0.mediaTitle,
                kind: first.0.mediaKind,
                daysAhead: key.daysAhead,
                expectedDay: first.0.expectedDay,
                units: units,
                presentation: blocking,
                progress: downloading.isEmpty ? nil : downloading.reduce(0, +) / Double(downloading.count),
                readyAt: ready.max()
            )
        }
    }

    // MARK: Hero

    /// Hero 的几张：先讲正在发生的，再讲刚到的，再讲今天和最近一次的预告；一部作品只占一张。
    /// 什么都没发生时退回「正在追踪」的几部，页面不至于只剩一排排海报。
    static func heroSlides(
        subscriptions: [API.SubscriptionView],
        groups: [ArrivalGroup],
        recent: [API.RecentArrivalView],
        now: Date
    ) -> [SubsHomeHeroSlide] {
        let subsById = Dictionary(subscriptions.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })

        let pipeline = groups
            .filter { $0.presentation.stageOrder > 0 }
            .sorted { left, right in
                // 整理中比下载中更快落地；同阶段按预计时间
                if left.presentation.stageOrder != right.presentation.stageOrder {
                    return left.presentation.stageOrder > right.presentation.stageOrder
                }
                return (left.presentation.estimatedAt ?? .distantFuture) < (right.presentation.estimatedAt ?? .distantFuture)
            }
        let arrived = recent.sorted { (importedAt($0) ?? .distantPast) > (importedAt($1) ?? .distantPast) }
        let fresh = arrived.filter { now.timeIntervalSince(importedAt($0) ?? .distantPast) <= freshArrivalWindow }
        let older = arrived.filter { now.timeIntervalSince(importedAt($0) ?? .distantPast) > freshArrivalWindow }
        let today = groups
            .filter { $0.daysAhead == 0 && $0.presentation.stageOrder == 0 }
            .sorted { ($0.presentation.estimatedAt ?? .distantFuture) < ($1.presentation.estimatedAt ?? .distantFuture) }
        let nearest = groups.filter { $0.daysAhead > 0 }.map(\.daysAhead).min()
        let upcoming = groups.filter { $0.daysAhead == nearest && $0.presentation.stageOrder == 0 }

        var slides: [SubsHomeHeroSlide] = []
        var seen = Set<Int>()
        func append(_ slide: SubsHomeHeroSlide?) {
            guard let slide, slides.count < maxHeroSlides, !seen.contains(slide.subscriptionId) else { return }
            seen.insert(slide.subscriptionId)
            slides.append(slide)
        }
        for group in pipeline { append(slide(group, media: subsById[group.subscriptionId]?.media, now: now)) }
        for card in fresh { append(slide(card, now: now)) }
        for group in today { append(slide(group, media: subsById[group.subscriptionId]?.media, now: now)) }
        for card in older { append(slide(card, now: now)) }
        for group in upcoming { append(slide(group, media: subsById[group.subscriptionId]?.media, now: now)) }

        if slides.isEmpty {
            // 退路：在追的优先，其次最近动过的；讲它处在什么状态，不编时间
            let ranked = subscriptions.sorted { left, right in
                let l = left.status == "active" ? 0 : 1, r = right.status == "active" ? 0 : 1
                if l != r { return l < r }
                return left.updatedAt > right.updatedAt
            }
            for sub in ranked.prefix(3) { append(restingSlide(sub)) }
        }
        return slides
    }

    private static func slide(_ group: ArrivalGroup, media: API.MediaBrief?, now: Date) -> SubsHomeHeroSlide? {
        guard let media else { return nil }
        let pres = group.presentation
        let label = group.episodeLabel
        var slide = SubsHomeHeroSlide(
            subscriptionId: group.subscriptionId,
            media: media,
            stage: .today,
            eyebrow: SubsHomeChip(text: "今天更新", tone: .today)
        )
        switch pres.statusLabel {
        case "下载中":
            slide.stage = .downloading
            let percent = group.progress.map { " · \(Int(($0 * 100).rounded()))%" } ?? ""
            slide.eyebrow = SubsHomeChip(text: "下载中\(percent)", tone: .live, pulse: true)
            slide.progress = group.progress
            if let eta = pres.estimatedAt {
                slide.clockLabel = group.kind == "movie" ? "预计可看" : "\(label) · 预计可看"
                slide.clock = clockText(eta, now: now)
            } else {
                slide.detail = label
                slide.footnote = "下载完成后自动整理入库"
            }
        case "整理中":
            slide.stage = .organizing
            slide.eyebrow = SubsHomeChip(text: "整理中", tone: .ok, pulse: true)
            // 已下载完、正在整理入库：有历史耗时就给出预计能看的时刻，超时了只说「马上就好」
            if let ready = group.readyAt, ready > now {
                slide.clockLabel = group.kind == "movie" ? "下载完成 · 预计可看" : "\(label) · 预计可看"
                slide.clock = clockText(ready, now: now)
            } else {
                slide.clockLabel = group.kind == "movie" ? "下载完成" : "\(label) · 下载完成"
                slide.clock = "马上就好"
            }
        default:
            if group.daysAhead > 0 {
                slide.stage = .upcoming
                slide.eyebrow = SubsHomeChip(text: "即将更新", tone: .calm)
                slide.clockLabel = [label, TodayArrivals.formatCalendarDay(group.expectedDay)].compactMap { $0 }.joined(separator: " · ")
                slide.clock = group.daysAhead == 1 ? "明天" : weekday(of: group.expectedDay) ?? "\(group.daysAhead) 天后"
            } else {
                slide.stage = .today
                if pres.statusLabel == "等待资源" {
                    slide.eyebrow = SubsHomeChip(text: "等待资源", tone: .warn)
                }
                if let estimated = pres.estimatedAt {
                    slide.clockLabel = "\(label) · 预计入库"
                    slide.clock = clockText(estimated, now: now)
                } else {
                    slide.detail = label
                    slide.footnote = pres.timeLabel
                }
            }
        }
        return slide
    }

    private static func slide(_ card: API.RecentArrivalView, now: Date) -> SubsHomeHeroSlide {
        let isTV = card.media.kind == "tv"
        let imported = importedAt(card)
        let fresh = imported.map { now.timeIntervalSince($0) <= 24 * 3600 } ?? false
        var footnote = imported.map { "\(Formatters.fromNow($0, now: now))入库" } ?? "已入库"
        if card.units.count > 1 { footnote += " · 共 \(card.units.count) 集新内容" }
        if let percent = card.progressPercent { footnote += " · 看到 \(percent)%" }
        return SubsHomeHeroSlide(
            subscriptionId: card.subscriptionId,
            media: card.media,
            stage: .arrived,
            eyebrow: SubsHomeChip(text: fresh ? "刚刚入库" : (isTV ? "新一集" : "新入库"), tone: .ok),
            detail: recentDetail(card),
            footnote: footnote,
            play: playRequest(card)
        )
    }

    private static func restingSlide(_ sub: API.SubscriptionView) -> SubsHomeHeroSlide {
        let eyebrow: SubsHomeChip = switch sub.status {
        case "paused": SubsHomeChip(text: "已暂停", tone: .calm)
        case "completed": SubsHomeChip(text: sub.media.kind == "movie" ? "已入库" : "已收齐", tone: .ok)
        default: SubsHomeChip(text: "追踪中", tone: .calm)
        }
        let meta = SubscriptionSummary.collectionMeta(sub)
        let detail: String
        let footnote: String?
        if sub.media.kind == "movie" {
            detail = [sub.media.year.map(String.init), "电影"].compactMap { $0 }.joined(separator: " · ")
            footnote = sub.progress.imported > 0 ? "已在媒体库里" : (released(sub) ? "找到合适的资源就会自动下载" : "上映后开始找资源")
        } else {
            detail = meta.map { "\($0.label) · \($0.value)" } ?? (sub.media.year.map(String.init) ?? "剧集")
            footnote = sub.status == "active" ? "有新一集会自动下载入库" : nil
        }
        return SubsHomeHeroSlide(subscriptionId: sub.id, media: sub.media, stage: .resting, eyebrow: eyebrow, detail: detail, footnote: footnote)
    }

    /// 刚刚入库卡的说明：剧集「S04E01 · 集名」，电影「2025 · 电影」
    static func recentDetail(_ card: API.RecentArrivalView) -> String {
        guard card.media.kind == "tv" else {
            return [card.media.year.map(String.init), "电影"].compactMap { $0 }.joined(separator: " · ")
        }
        let code = "S\(SubsFormat.pad(card.seasonNumber))E\(SubsFormat.pad(card.episodeNumber))"
        guard let name = card.episodeName, !name.isEmpty else { return code }
        return "\(code) · \(name)"
    }

    /// 刚刚入库 → 起播请求（入口就是这一批里第一个没看完的单元）
    static func playRequest(_ card: API.RecentArrivalView) -> PlayRequest {
        let isTV = card.media.kind == "tv"
        return PlayRequest(
            mediaItemId: card.media.mediaItemId,
            season: isTV ? card.seasonNumber : nil,
            episode: isTV ? card.episodeNumber : nil
        )
    }

    static func importedAt(_ card: API.RecentArrivalView) -> Date? { Formatters.date(card.importedAt) }

    /// 大号时刻：同一天只写时刻，跨天写「明天 08:10」「10/1 08:10」
    static func clockText(_ date: Date, now: Date) -> String {
        let calendar = Calendar.current
        let clock = SubsFormat.clock(date)
        if calendar.isDate(date, inSameDayAs: now) { return clock }
        if let tomorrow = calendar.date(byAdding: .day, value: 1, to: now), calendar.isDate(date, inSameDayAs: tomorrow) {
            return "明天 \(clock)"
        }
        let parts = calendar.dateComponents([.month, .day], from: date)
        return "\(parts.month ?? 0)/\(parts.day ?? 0) \(clock)"
    }

    // MARK: 日程

    private static let weekdayNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]

    /// 站点日历日 YYYY-MM-DD →「周四」（按日历日本身算，不经时区换算）
    static func weekday(of day: String) -> String? {
        guard let date = SubsFormat.date(day) else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC") ?? .gmt
        return weekdayNames[calendar.component(.weekday, from: date) - 1]
    }

    /// 一周的日期条：今天起连续 7 天（窗口第 8 天有安排才补上），没有安排的日子照样占位但点不了——
    /// 日历的节奏本身就是信息（「周二、周三都没有」）。一条预告都没有时整块不出现。
    static func scheduleDays(
        groups: [ArrivalGroup],
        subscriptions: [API.SubscriptionView],
        now: Date
    ) -> [SubsHomeScheduleDay] {
        guard let anchor = groups.first, let anchorDate = SubsFormat.date(anchor.expectedDay) else { return [] }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC") ?? .gmt
        guard let siteToday = calendar.date(byAdding: .day, value: -anchor.daysAhead, to: anchorDate) else { return [] }
        let subsById = Dictionary(subscriptions.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        let byDay = Dictionary(grouping: groups, by: \.daysAhead)
        let lastDay = byDay[7] == nil ? 6 : 7
        return (0 ... lastDay).compactMap { offset in
            guard let date = calendar.date(byAdding: .day, value: offset, to: siteToday) else { return nil }
            let parts = calendar.dateComponents([.month, .day, .weekday], from: date)
            let entries = (byDay[offset] ?? [])
                .map { entry($0, media: subsById[$0.subscriptionId]?.media, now: now) }
                .sorted { left, right in
                    if left.tone.glows != right.tone.glows { return left.tone.glows }
                    return (left.time ?? "99") < (right.time ?? "99")
                }
            return SubsHomeScheduleDay(
                daysAhead: offset,
                weekday: offset == 0 ? "今天" : weekdayNames[(parts.weekday ?? 1) - 1],
                dayNumber: "\(parts.day ?? 0)",
                dateLabel: "\(parts.month ?? 0)月\(parts.day ?? 0)日",
                entries: entries
            )
        }
    }

    private static func entry(_ group: ArrivalGroup, media: API.MediaBrief?, now: Date) -> SubsHomeScheduleEntry {
        let pres = group.presentation
        let tone: SubsHomeTone = switch pres.statusLabel {
        case "下载中": .live
        case "整理中": .ok
        case "等待资源": .warn
        default: .calm
        }
        var status = pres.statusLabel
        if pres.statusLabel == "下载中", let progress = group.progress {
            status += " \(Int((progress * 100).rounded()))%"
        }
        let time: String? = switch pres.statusLabel {
        // 整理超时（过了预计时刻还没入库）不再报一个过去的时刻
        case "整理中": group.readyAt.flatMap { $0 > now ? SubsFormat.clock($0) : nil } ?? "即将"
        // 下载器给不出 ETA 时，已经在路上的东西说「稍后」，比「待定」更贴切
        case "下载中": pres.estimatedAt.map(SubsFormat.clock) ?? "稍后"
        default: pres.estimatedAt.map(SubsFormat.clock)
        }
        return SubsHomeScheduleEntry(
            subscriptionId: group.subscriptionId,
            title: group.title,
            episodeLabel: group.episodeLabel,
            time: time,
            status: status,
            tone: tone,
            progress: pres.statusLabel == "下载中" ? group.progress : nil,
            media: media
        )
    }

    // MARK: 海报行

    /// 剧集 / 电影一排：在追的按「此刻最要紧」排前面，已收齐 / 已暂停压暗排在分隔线后面
    static func shelf(
        kind: String,
        subscriptions: [API.SubscriptionView],
        groups: [ArrivalGroup],
        recent: [API.RecentArrivalView]
    ) -> SubsHomeShelf {
        // 每部作品只看它最近的那一天
        var nearestGroup: [Int: ArrivalGroup] = [:]
        for group in groups where nearestGroup[group.subscriptionId].map({ group.daysAhead < $0.daysAhead }) ?? true {
            nearestGroup[group.subscriptionId] = group
        }
        let recentBySub = Dictionary(recent.map { ($0.subscriptionId, $0) }, uniquingKeysWith: { first, _ in first })

        var ranked: [(rank: Double, item: SubsHomeShelfItem)] = []
        var resting: [SubsHomeShelfItem] = []
        for sub in subscriptions where sub.media.kind == kind {
            let (chip, rank) = chipAndRank(sub, group: nearestGroup[sub.id], recent: recentBySub[sub.id])
            let isResting = rank == nil
            let item = SubsHomeShelfItem(
                sub: sub,
                chip: chip,
                meta: meta(sub, resting: isResting),
                progress: isResting ? nil : seasonProgress(sub),
                resting: isResting
            )
            if let rank { ranked.append((rank, item)) } else { resting.append(item) }
        }
        let active = ranked.sorted { left, right in
            if left.rank != right.rank { return left.rank < right.rank }
            if left.item.sub.updatedAt != right.item.sub.updatedAt { return left.item.sub.updatedAt > right.item.sub.updatedAt }
            return left.item.sub.media.title < right.item.sub.media.title
        }.map(\.item)
        // 歇着的：暂停在前（还能恢复），收齐的按最近动过排
        let rest = resting.sorted { left, right in
            let l = left.sub.status == "paused" ? 0 : 1, r = right.sub.status == "paused" ? 0 : 1
            if l != r { return l < r }
            return left.sub.updatedAt > right.sub.updatedAt
        }
        let hasPaused = rest.contains { $0.sub.status == "paused" }
        let hasDone = rest.contains { $0.sub.status != "paused" }
        let label = hasPaused && hasDone ? "暂停·收齐" : (hasPaused ? "已暂停" : (kind == "movie" ? "已入库" : "已收齐"))
        return SubsHomeShelf(active: active, resting: rest, restingLabel: label)
    }

    /// 一排的计数：「5 部追踪中 · 共 12 部」（全在追或一部都不在追时只说总数）。
    /// 按订阅状态数，首页与海报墙同一口径——「排在前面」与「追踪中」不是一回事
    static func countSummary(_ shelf: SubsHomeShelf) -> String {
        let total = shelf.all.count
        let tracking = shelf.all.filter { $0.sub.status == "active" }.count
        guard tracking > 0, tracking < total else { return "共 \(total) 部" }
        return "\(tracking) 部追踪中 · 共 \(total) 部"
    }

    /// 一张海报的小签与排位；排位 nil = 歇着（已收齐 / 已暂停），排到分隔线后
    static func chipAndRank(
        _ sub: API.SubscriptionView,
        group: ArrivalGroup?,
        recent: API.RecentArrivalView?
    ) -> (SubsHomeChip?, Double?) {
        let isTV = sub.media.kind == "tv"
        if sub.status == "paused" {
            return (SubsHomeChip(text: "已暂停", tone: .calm), nil)
        }
        if let group {
            switch group.presentation.statusLabel {
            case "下载中": return (SubsHomeChip(text: "下载中", tone: .live, pulse: true), 0)
            case "整理中": return (SubsHomeChip(text: "整理中", tone: .ok, pulse: true), 0)
            default: break
            }
        } else if sub.progress.downloaded > 0 {
            return (SubsHomeChip(text: "整理中", tone: .ok, pulse: true), 0)
        } else if sub.progress.grabbed > 0 {
            return (SubsHomeChip(text: "下载中", tone: .live, pulse: true), 0)
        }
        if let recent {
            let count = recent.units.count
            let text = isTV ? (count > 1 ? "新 \(count) 集" : "新一集") : "新入库"
            return (SubsHomeChip(text: text, tone: .ok), 1)
        }
        if let group {
            if group.daysAhead == 0 { return (SubsHomeChip(text: "今天更新", tone: .today), 2) }
            let when = group.daysAhead == 1 ? "明天" : (weekday(of: group.expectedDay) ?? "\(group.daysAhead) 天后")
            return (SubsHomeChip(text: "\(when)更新", tone: .calm), 3 + Double(group.daysAhead) / 100)
        }
        if sub.progress.upgrading > 0 {
            return (SubsHomeChip(text: "洗版中", tone: .upgrade), 4)
        }
        if SubscriptionSummary.fullyCollected(sub) || sub.status == "completed" {
            return (nil, nil)
        }
        if !isTV {
            return released(sub) ? (SubsHomeChip(text: "找资源中", tone: .warn), 5) : (SubsHomeChip(text: "未上映", tone: .calm), 6)
        }
        return (nil, 5)
    }

    /// 海报下第二行
    static func meta(_ sub: API.SubscriptionView, resting: Bool) -> String {
        let year = sub.media.year.map(String.init)
        if sub.media.kind == "movie" {
            if sub.progress.imported > 0 { return [year, "已入库"].compactMap { $0 }.joined(separator: " · ") }
            return year ?? "电影"
        }
        guard let meta = SubscriptionSummary.collectionMeta(sub) else { return year ?? "剧集" }
        if resting, sub.status != "paused" { return "已收齐 · \(meta.label)" }
        return "\(meta.label) · \(meta.value)"
    }

    /// 剧集当季收录比例（海报底部细线）：在追的看最新一季，与收录摘要同口径
    static func seasonProgress(_ sub: API.SubscriptionView) -> Double? {
        guard sub.media.kind == "tv" else { return nil }
        let seasons = sub.seasonCollection.filter { $0.seasonNumber > 0 }.sorted { $0.seasonNumber < $1.seasonNumber }
        let selected = Set(sub.selectedSeasons.filter { $0 > 0 })
        let scoped = selected.isEmpty ? seasons : seasons.filter { selected.contains($0.seasonNumber) }
        guard let latest = (sub.followFuture ? seasons.last : nil) ?? scoped.last else { return nil }
        let total = max(latest.episodeCount ?? 0, latest.airedCount, latest.ownedCount)
        guard total > 0 else { return nil }
        return min(1, Double(latest.ownedCount) / Double(total))
    }

    /// 电影是否已上映（TMDB status；缺失按已上映处理——宁可说「找资源中」也不误报「未上映」）
    static func released(_ sub: API.SubscriptionView) -> Bool {
        guard let status = sub.media.status, !status.isEmpty else { return true }
        return status == "Released"
    }
}
