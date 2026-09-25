import SwiftUI

// 媒体库模块内共用的小工具：类型文案、扫描阶段、季集摘要、观看记录/标记接口封装。
// 口径逐条对齐 Web（lib/api/libraries.ts、lib/recent-addition.ts、lib/library-inventory-summary.ts、
// lib/favorites.ts），改文案请两端一起改。

/// 库类型的展示名与图标（Web `LIBRARY_KIND_META`）
enum LibraryKindMeta {
    static func label(_ kind: String) -> String {
        switch kind {
        case "movie": "电影"
        case "tv": "剧集"
        case "video": "其他"
        case "photo": "图片"
        default: kind
        }
    }

    static func symbol(_ kind: String) -> String {
        switch kind {
        case "movie": "film"
        case "tv": "tv"
        case "video": "video"
        case "photo": "photo"
        default: "square.stack"
        }
    }
}

/// 扫描阶段 → 状态词（Web `SCAN_PHASE_LABELS`）。扫描内部分阶段，阶段变了进度文案必须跟着变，
/// 否则文件扫完后还在补图，环停在 100% 配「扫描中」看起来就是卡死了。
enum ScanPhase {
    static func label(_ phase: String?) -> String {
        switch phase ?? "ingesting" {
        case "walking": "正在盘点文件"
        case "ingesting": "正在扫描"
        case "probing": "正在补探画质与音轨"
        case "assets": "正在补齐海报与剧照"
        case "reidentifying": "正在重新识别条目"
        case "organizing": "正在整理文件名"
        default: "扫描中"
        }
    }

    static func hint(_ phase: String?) -> String {
        switch phase ?? "ingesting" {
        case "walking": "正在统计待处理的文件数"
        case "ingesting": "识别到的内容会自动入库"
        case "probing": "文件已全部入库，正在读取文件本体的规格"
        case "assets": "文件已全部入库，正在下载图片"
        case "reidentifying": "完成后条目身份会更新"
        case "organizing": "完成后自动刷新"
        default: ""
        }
    }
}

private func pad2(_ value: Int) -> String { String(format: "%02d", value) }

/// 「最近添加」的季集摘要（Web `formatRecentAddition`）：连续区间保留精确集号，零散或跨季改为计数。
func formatRecentAddition(_ addition: API.LibraryRecentAdditionView) -> String? {
    if addition.episodeCount == 0 || addition.seasonCount == 0 { return nil }
    if addition.seasonCount > 1 { return "新增\(addition.seasonCount)季 · \(addition.episodeCount)集" }
    guard let season = addition.seasonNumber else { return nil }
    if addition.completeSeason {
        return "\(season == 0 ? "特别篇" : "第\(season)季") · 全\(addition.episodeCount)集"
    }
    guard let start = addition.firstEpisodeNumber, let end = addition.lastEpisodeNumber else {
        return "S\(pad2(season)) · 新增\(addition.episodeCount)集"
    }
    return start == end ? "S\(pad2(season))E\(pad2(start))" : "S\(pad2(season))E\(pad2(start))–\(pad2(end))"
}

/// 剧集库存概况（Web `formatLibraryInventorySummary`）
func formatInventorySummary(_ summary: API.LibraryInventorySummaryView) -> String {
    let episodes: String
    if summary.allEpisodesOwned {
        episodes = "全 \(summary.episodeCount) 集"
    } else if summary.seasonNumber != nil, let total = summary.totalEpisodeCount {
        episodes = "\(summary.episodeCount)/\(total) 集"
    } else {
        episodes = "\(summary.episodeCount) 集"
    }
    if let season = summary.seasonNumber {
        return "\(season == 0 ? "特别篇" : "第 \(season) 季") · \(episodes)"
    }
    let seasons = summary.allSeasonsOwned ? "全 \(summary.seasonCount) 季" : "共 \(summary.seasonCount) 季"
    return "\(seasons) · \(episodes)"
}

/// 库存条目的订阅动作（Web `libraryInventoryAction`）：季集未齐 →「补齐缺集」，齐了 →「自动续订」
enum LibraryInventoryAction {
    case follow, backfill

    static func of(kind: String, summary: API.LibraryInventorySummaryView?) -> LibraryInventoryAction? {
        guard kind == "tv", let summary else { return nil }
        return summary.allSeasonsOwned && summary.allEpisodesOwned ? .follow : .backfill
    }

    var label: String { self == .follow ? "自动续订" : "补齐缺集" }
    var systemImage: String { self == .follow ? "bell" : "arrow.down.circle" }
}

/// 收藏层级说明（Web `favoriteLevelLabel`）：整剧与电影不解释
func favoriteLevelLabel(kind: String, season: Int?, episode: Int?) -> String? {
    guard kind == "tv", let season else { return nil }
    if let episode { return "收藏了 S\(pad2(season))E\(pad2(episode))" }
    return season == 0 ? "收藏了特别篇" : "收藏了第 \(season) 季"
}

/// 字节 → 「19.40 GB」（Web `formatBytes` 口径：1024 进制、保留两位，≥100 或 B 取整）
func libraryBytes(_ bytes: Int?) -> String {
    guard let bytes, bytes >= 0 else { return "—" }
    let units = ["B", "KB", "MB", "GB", "TB", "PB"]
    var value = Double(bytes)
    var i = 0
    while value >= 1024, i < units.count - 1 {
        value /= 1024
        i += 1
    }
    let rounded = (value * 100).rounded() / 100
    return String(format: rounded >= 100 || i == 0 ? "%.0f %@" : "%.2f %@", rounded, units[i])
}

/// 相对时间（Web dayjs `fromNow` 中文口径）：「几秒前」「18 天前」「1 个月前」「3 天内」
func libraryFromNow(_ raw: String?) -> String {
    guard let date = Formatters.date(raw) else { return "从未" }
    let delta = Date.now.timeIntervalSince(date)
    let future = delta < 0
    let s = abs(delta)
    let text: String
    switch s {
    case ..<45: text = "几秒"
    case ..<90: text = "1 分钟"
    case ..<(45 * 60): text = "\(Int((s / 60).rounded())) 分钟"
    case ..<(90 * 60): text = "1 小时"
    case ..<(22 * 3600): text = "\(Int((s / 3600).rounded())) 小时"
    case ..<(36 * 3600): text = "1 天"
    case ..<(26 * 86400): text = "\(Int((s / 86400).rounded())) 天"
    case ..<(46 * 86400): text = "1 个月"
    case ..<(320 * 86400): text = "\(max(2, Int((s / 86400 / 30.4).rounded()))) 个月"
    case ..<(548 * 86400): text = "1 年"
    default: text = "\(Int((s / 86400 / 365).rounded())) 年"
    }
    return future ? "\(text)内" : "\(text)前"
}

/// S01E02 形式的集号
func episodeCode(season: Int, episode: Int) -> String { "S\(pad2(season))E\(pad2(episode))" }

/// 媒体库首页摘要（Web `libraryStatsSummary`）：只聚合库列表随带的统计快照
func libraryStatsSummary(_ libraries: [API.LibraryView]?) -> String {
    guard let libraries else { return "正在汇总媒体库统计…" }
    if libraries.isEmpty { return "还没有媒体库，创建后会在这里显示库存统计" }
    func count(_ kind: String) -> Int { libraries.filter { $0.kind == kind }.reduce(0) { $0 + $1.stats.itemCount } }
    let size = libraries.reduce(0) { $0 + $1.stats.totalSizeBytes }
    let video = count("video")
    let videoPart = video > 0 ? " · \(video) 个其他视频" : ""
    return "\(libraries.count) 个媒体库 · \(count("movie")) 部电影 · \(count("tv")) 部剧集\(videoPart) · 共占用 \(libraryBytes(size)) 存储空间"
}

// MARK: - 接口封装（需要信封 message 或生成器没覆盖的组合）

nonisolated extension APIClient {
    /// 清除观看记录：返回结果与后端的中文提示（Web 直接把 message 当 Toast）
    func libraryClearHistory(scope: String, mediaItemId: Int? = nil, libraryId: Int? = nil, since: Date? = nil) async throws -> (result: API.PlaybackHistoryClearView, message: String) {
        var query = [URLQueryItem(name: "scope", value: scope)]
        if let mediaItemId { query.append(URLQueryItem(name: "media_item_id", value: String(mediaItemId))) }
        if let libraryId { query.append(URLQueryItem(name: "library_id", value: String(libraryId))) }
        if let since {
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            query.append(URLQueryItem(name: "since", value: formatter.string(from: since)))
        }
        let envelope: APIEnvelope<API.PlaybackHistoryClearView> = try await raw("DELETE", "/playback/history", query: query)
        return (envelope.data, envelope.message ?? "已清除观看记录")
    }

    /// 设置收藏 / 已看标记（Web `setPlaybackMarks`）
    func librarySetMarks(mediaItemId: Int, season: Int? = nil, episode: Int? = nil, played: Bool? = nil, favorite: Bool? = nil) async throws -> API.PlaybackMarksView {
        try await playbackMarksSet(body: API.PlaybackMarksRequest(
            mediaItemId: mediaItemId, seasonNumber: season, episodeNumber: episode,
            played: played, favorite: favorite, deviceId: nil
        ))
    }
}

// MARK: - 海报墙排序（单库页 / 合集 / 收藏共用的「档位 + 方向」模型）

/// 一档排序的方向档案（Web `SortDirection`）：自然方向与两个方向的人话；nil = 没有方向
struct WallSortDirection: Hashable {
    var naturalAsc: Bool
    var asc: String
    var desc: String

    /// 反转了自然方向才带 order（与 Web orderParam 同一条规矩）
    func orderParam(reversed: Bool) -> String? {
        guard reversed else { return nil }
        return naturalAsc ? "desc" : "asc"
    }

    /// 当前方向的人话
    func label(reversed: Bool) -> String {
        let asc = naturalAsc != reversed
        return asc ? self.asc : desc
    }

    /// 存下来的 order → 是否反转
    func isReversed(order: String?) -> Bool {
        guard let order else { return false }
        return (order == "asc") != naturalAsc
    }
}

/// 海报墙各档排序的方向档案（Web lib/wall-sort.ts `SORT_DIRECTIONS`）
enum WallSortDirections {
    static func of(_ sort: String) -> WallSortDirection? {
        switch sort {
        case "title", "probing": WallSortDirection(naturalAsc: true, asc: "A→Z", desc: "Z→A")
        case "added_at", "release_date", "favorited_at": WallSortDirection(naturalAsc: false, asc: "旧→新", desc: "新→旧")
        case "rating": WallSortDirection(naturalAsc: false, asc: "低→高", desc: "高→低")
        case "runtime": WallSortDirection(naturalAsc: true, asc: "短→长", desc: "长→短")
        case "size": WallSortDirection(naturalAsc: false, asc: "小→大", desc: "大→小")
        case "last_played": WallSortDirection(naturalAsc: false, asc: "远→近", desc: "近→远")
        default: nil
        }
    }
}

/// 排序选择（档位 + 是否反转），按 key 存进 UserDefaults 记忆（Web 用 localStorage）
struct WallSortState: Hashable, Codable {
    var sort: String
    var reversed: Bool = false

    static func load(_ key: String, default fallback: WallSortState, allowed: [String]) -> WallSortState {
        guard let data = UserDefaults.standard.data(forKey: key),
              let saved = try? JSONDecoder().decode(WallSortState.self, from: data),
              allowed.contains(saved.sort) else { return fallback }
        return saved
    }

    func save(_ key: String) {
        if let data = try? JSONEncoder().encode(self) { UserDefaults.standard.set(data, forKey: key) }
    }
}

/// 排序菜单：每个档位一项，当前档位再点一次翻转方向（Web WallSortControl 的原生写法）
struct WallSortMenu: View {
    struct Option: Hashable {
        var value: String
        var label: String
        var direction: WallSortDirection?
    }

    let options: [Option]
    @Binding var state: WallSortState

    var body: some View {
        let current = options.first { $0.value == state.sort } ?? options.first
        Menu {
            ForEach(options, id: \.value) { option in
                Button {
                    if option.value == state.sort, option.direction != nil {
                        state.reversed.toggle()
                    } else {
                        state = WallSortState(sort: option.value, reversed: false)
                    }
                } label: {
                    if option.value == state.sort {
                        Label(option.label + (option.direction.map { " · \($0.label(reversed: state.reversed))" } ?? ""), systemImage: "checkmark")
                    } else {
                        Text(option.label)
                    }
                }
            }
            if let direction = current?.direction {
                Divider()
                Button {
                    state.reversed.toggle()
                } label: {
                    Label("方向：\(direction.label(reversed: state.reversed))", systemImage: "arrow.up.arrow.down")
                }
            }
        } label: {
            HStack(spacing: 4) {
                Text(current?.label ?? "排序").font(.subheadline.weight(.semibold))
                if let direction = current?.direction {
                    Image(systemName: direction.naturalAsc != state.reversed ? "arrow.up" : "arrow.down")
                        .font(.caption.weight(.semibold))
                }
                Image(systemName: "chevron.down").font(.caption2.weight(.semibold)).foregroundStyle(Theme.textMuted)
            }
            .foregroundStyle(Theme.text)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
        .accessibilityIdentifier("wall-sort")
    }
}
