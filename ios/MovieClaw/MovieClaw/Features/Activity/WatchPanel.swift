import SwiftUI

/// 「观看」视角主体（Web `MediaActivityPanel`）：工具栏（三个切片 + 作用于整个切片的筛选）+ 切片内容。
///
/// 三个切片时间语义与刷新节奏都不同：正在播放是实时快照（8 秒轮询，数据在外壳常驻的 store 里）；
/// 最近播放每场一行、按游标分页；观看统计按周期汇总。筛选条件站在内容之上同一行——
/// 周期只对统计有效，成员对记录与统计有效，范围（我的浏览范围 / 全部）管整个观看视角。
struct WatchPanel: View {
    let store: MediaActivityStore
    @Binding var view: WatchSlice

    @Environment(\.api) private var api
    @State private var memberId: Int?
    @State private var days = 30
    @State private var members: [API.MemberView] = []

    static let periods: [ActivityFilterMenu<Int>.Option] = [
        .init(value: 7, label: "最近 7 天"), .init(value: 30, label: "最近 30 天"), .init(value: 90, label: "最近 90 天"),
    ]

    private static let scopeOptions: [ActivityFilterMenu<String>.Option] = [
        .init(value: "visible", label: "我的浏览范围", hint: "对自己隐藏的库只报个数，不出片名"),
        .init(value: "all", label: "全部", hint: "跨成员、跨库，管理视角"),
    ]

    private var memberOptions: [ActivityFilterMenu<Int>.Option] {
        [.init(value: -1, label: "全部成员"), .init(value: 0, label: "超级管理员")]
            + members.map { .init(value: $0.id, label: $0.nickname.isEmpty ? $0.username : $0.nickname) }
    }

    private var memberLabel: String? {
        guard let memberId else { return nil }
        return memberOptions.first { $0.value == memberId }?.label
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            toolbar
            if let error = store.error {
                ActivityWarningBanner(message: error).padding(.top, 16)
            }
            switch view {
            case .playing:
                NowPlayingSection(store: store, onShowPlays: { view = .plays })
                    .padding(.top, 16)
            case .plays:
                PlaybackHistoryList(
                    scope: store.scope, memberId: memberId, memberLabel: memberLabel,
                    onClearMember: { memberId = nil }, onShowAll: { store.setScope("all") }
                )
                .padding(.top, 16)
            case .stats:
                WatchStatsPanel(
                    scope: store.scope, days: days, memberId: memberId, memberLabel: memberLabel,
                    onMemberSelect: { memberId = $0 }, onDaysChange: { days = $0 },
                    onShowAll: { store.setScope("all") }
                )
                .padding(.top, 16)
            }
        }
        .task(id: view) {
            // 成员候选只在「最近播放 / 观看统计」用得上，进到那片再拉一次
            guard view != .playing, members.isEmpty else { return }
            members = (try? await api.membersList()) ?? []
        }
    }

    private var toolbar: some View {
        VStack(alignment: .leading, spacing: 10) {
            ActivitySliceTabs(
                slices: WatchSlice.allCases, selection: view, label: \.label,
                count: { $0 == .playing ? store.liveCount : nil },
                identifier: { "watch-view-\($0.rawValue)" },
                onSelect: { view = $0 }
            )
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    if view == .stats {
                        ActivityFilterMenu(label: "周期", value: days, options: Self.periods) { days = $0 }
                    }
                    if view != .playing {
                        ActivityFilterMenu(label: "成员", value: memberId ?? -1, options: memberOptions) {
                            memberId = $0 < 0 ? nil : $0
                        }
                    }
                    ActivityFilterMenu(label: "范围", value: store.scope, options: Self.scopeOptions) { store.setScope($0) }
                }
            }
            .scrollClipDisabled()
            Rectangle().fill(Color.white.opacity(0.08)).frame(height: 1)
        }
        .padding(.top, 18)
    }
}

// MARK: - 正在播放

/// 正在播放：会话卡片 + 范围外折叠行 + 「正在下载」分区（只在真有下载时出现）
struct NowPlayingSection: View {
    let store: MediaActivityStore
    let onShowPlays: () -> Void

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var busyDevice: String?

    var body: some View {
        let snapshot = store.snapshot
        VStack(alignment: .leading, spacing: 10) {
            if store.loading, store.liveCount == 0 {
                HStack(spacing: 10) {
                    ProgressView()
                    Text("正在读取媒体库活动…").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 60)
            } else if snapshot.sessions.isEmpty, snapshot.hiddenSessionCount == 0 {
                ActivityEmptyCard(
                    systemImage: "play.fill", title: "现在没有人在看",
                    message: "设备开始播放后几秒内会出现在这里，网页播放器和 Jellyfin 客户端都算；想看之前谁看了什么，去最近播放。"
                ) {
                    ActivityPillButton(title: "查看最近播放", action: onShowPlays)
                }
                .accessibilityIdentifier("watch-empty")
            } else {
                ForEach(snapshot.sessions, id: \.self) { session in
                    PlaybackSessionCard(session: session, busy: busyDevice != nil, onEnd: end, onRevoke: revoke)
                }
                if snapshot.hiddenSessionCount > 0 {
                    HiddenCountRow(count: snapshot.hiddenSessionCount, noun: "台设备在播放的内容") { store.setScope("all") }
                        .activityHiddenRowCard()
                }
            }

            if !snapshot.downloads.isEmpty || snapshot.hiddenDownloadCount > 0 {
                ActivitySectionHeading(
                    systemImage: "arrow.down.circle", title: "正在下载",
                    count: snapshot.downloads.count + snapshot.hiddenDownloadCount
                )
                .padding(.top, 18)
                ForEach(snapshot.downloads, id: \.self) { download in
                    FileDownloadCard(download: download, busy: busyDevice != nil, onRevoke: revoke)
                }
                if snapshot.hiddenDownloadCount > 0 {
                    HiddenCountRow(count: snapshot.hiddenDownloadCount, noun: "条下载") { store.setScope("all") }
                        .activityHiddenRowCard()
                }
            }
        }
    }

    /// 结束播放：可逆（再点播放即可继续），但会打断别人正在看的东西，问一句
    private func end(deviceId: String, label: String) {
        Task {
            let confirmed = await feedback.confirm(
                "结束「\(label)」本次播放？",
                message: "这台设备正在进行的播放会立刻中断，一分钟内不能续播；登录凭据与观看进度都不受影响，之后重新点播放即可继续。",
                confirmTitle: "结束播放"
            )
            guard confirmed else { return }
            await perform(deviceId, fallback: "结束播放失败") { try await api.activityEndPlayback(deviceId: deviceId) }
        }
    }

    /// 注销设备：设备要重新登录（电视上尤其麻烦），显式确认
    private func revoke(deviceId: String, label: String) {
        Task {
            let confirmed = await feedback.confirm(
                "注销这台设备？",
                message: "「\(label)」的登录凭据会立刻失效，正在进行的播放与下载一并停止，该设备下次使用需要重新登录。\n\n已看进度、收藏这些观看记录按账号保存，不会因为注销设备而丢失。",
                confirmTitle: "注销设备",
                destructive: true
            )
            guard confirmed else { return }
            await perform(deviceId, fallback: "注销设备失败") { try await api.activityRevokeDevice(deviceId: deviceId) }
        }
    }

    private func perform(_ deviceId: String, fallback: String, _ call: () async throws -> String) async {
        guard busyDevice == nil else { return }
        busyDevice = deviceId
        defer { busyDevice = nil }
        do {
            feedback.success(try await call())
            store.refresh()
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? fallback : error.localizedDescription)
        }
    }
}

nonisolated extension APIClient {
    /// 结束一台设备本次播放（Web 把后端 message 当 Toast，这里同样返回它）
    func activityEndPlayback(deviceId: String) async throws -> String {
        let envelope: APIEnvelope<API.JSONValue?> = try await raw("POST", "/playback/activity/sessions/\(deviceId)/end")
        return envelope.message ?? "已结束播放"
    }

    /// 注销一台播放设备
    func activityRevokeDevice(deviceId: String) async throws -> String {
        let envelope: APIEnvelope<API.JSONValue?> = try await raw("DELETE", "/playback/devices/\(deviceId)")
        return envelope.message ?? "设备已注销"
    }
}

// MARK: - 卡片

enum WatchFormat {
    static func unitLabel(_ media: API.MediaActivityTarget) -> String? {
        guard media.kind == "tv" else { return nil }
        return "S\(TaskCenter.pad2(media.seasonNumber))E\(TaskCenter.pad2(media.episodeNumber))"
    }

    static func deviceLabel(client: String, deviceName: String) -> String {
        if !client.isEmpty, !deviceName.isEmpty, client != deviceName { return "\(client) · \(deviceName)" }
        return deviceName.isEmpty ? (client.isEmpty ? "未知设备" : client) : deviceName
    }

    /// 可跳详情的前提：有落点库且在超管的可浏览范围内（否则详情接口 404）
    static func detailRoute(_ media: API.MediaActivityTarget) -> AppRoute? {
        guard let libraryId = media.libraryId, media.browsable else { return nil }
        return .libraryItem(libraryId: libraryId, itemId: media.mediaItemId)
    }

    static func metaLine(_ parts: [String?]) -> String {
        parts.compactMap { $0?.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

/// 作品标题：片名 年份 SxxEyy 单集名 [仅管理]；可跳详情时整块可点
struct ActivityTitleText: View {
    let media: API.MediaActivityTarget
    var episode = true
    var large = false
    var showYear = true

    @Environment(Router.self) private var router

    var body: some View {
        let route = WatchFormat.detailRoute(media)
        Button {
            if let route { router.open(route) }
        } label: {
            titleText
                .font(large ? .system(size: 17, weight: .bold) : .subheadline.weight(.semibold))
                .foregroundStyle(Theme.text.opacity(0.9))
                .lineLimit(large ? 2 : 1)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(.plain)
        .disabled(route == nil)
    }

    private var titleText: Text {
        var parts = [Text(media.title.isEmpty ? "（条目已删除）" : media.title)]
        if showYear, let year = media.year {
            parts.append(Text(" \(String(year))").fontWeight(.regular).foregroundStyle(Theme.textFaint))
        }
        if episode, let unit = WatchFormat.unitLabel(media) {
            parts.append(Text(" \(unit)").fontWeight(.regular).foregroundStyle(Theme.textMuted).monospacedDigit())
        }
        if episode, let title = media.episodeTitle, !title.isEmpty {
            parts.append(Text(" \(title)").fontWeight(.regular).foregroundStyle(Theme.textFaint))
        }
        // 会话卡（带年份）范围外即标「仅管理」；记录 / 统计行只在有落点库时标（同 Web TitleText）
        if !media.browsable, media.libraryId != nil || showYear {
            parts.append(Text("  仅管理").font(.caption2.weight(.medium)).foregroundStyle(Theme.textFaint))
        }
        return Text.activityJoin(parts)
    }
}

/// 会话 / 下载卡的海报位；可跳详情时可点
struct ActivityPoster: View {
    let media: API.MediaActivityTarget?
    var width: CGFloat = 52
    var height: CGFloat = 76
    var radius: CGFloat = 8

    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    var body: some View {
        let route = media.flatMap(WatchFormat.detailRoute)
        Button {
            if let route { router.open(route) }
        } label: {
            RemoteImage(url: api.image(media?.posterUrl, .posterCard), placeholderSymbol: "film")
                .frame(width: width, height: height)
                .clipShape(.rect(cornerRadius: radius))
                .overlay(RoundedRectangle(cornerRadius: radius).strokeBorder(Color.white.opacity(0.1)))
        }
        .buttonStyle(.plain)
        .disabled(route == nil)
    }
}

/// 会话 / 下载共用的卡片外壳：进度条贴卡片底边横跨全宽（多张卡共享同一条基线，便于比较）
private struct ActivityCardShell<Content: View>: View {
    var percent: Int?
    var muted = false
    @ViewBuilder var content: () -> Content

    var body: some View {
        HStack(alignment: .top, spacing: 12) { content() }
            .padding(12)
            .padding(.bottom, 2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.03))
            .overlay(alignment: .bottom) {
                if let percent {
                    GeometryReader { proxy in
                        ZStack(alignment: .leading) {
                            Color.white.opacity(0.06)
                            (muted ? Color.white.opacity(0.3) : Theme.info)
                                .frame(width: proxy.size.width * CGFloat(min(100, max(1, percent))) / 100)
                        }
                    }
                    .frame(height: 3)
                }
            }
            .clipShape(.rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.08)))
    }
}

/// 设备操作菜单：结束播放（只掐断本次播放）、注销此设备（仅持 Jellyfin 凭据的会话）
private struct DeviceActionsMenu: View {
    let deviceId: String
    let label: String
    var onEnd: ((String, String) -> Void)?
    var onRevoke: ((String, String) -> Void)?
    let busy: Bool

    var body: some View {
        if onEnd != nil || onRevoke != nil {
            Menu {
                if let onEnd {
                    Button("结束播放", systemImage: "stop.circle") { onEnd(deviceId, label) }
                }
                if let onRevoke {
                    Button("注销此设备", systemImage: "rectangle.portrait.and.arrow.right", role: .destructive) { onRevoke(deviceId, label) }
                }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.textMuted)
                    .frame(width: 30, height: 30)
                    .contentShape(.rect)
            }
            .disabled(busy)
            .accessibilityLabel("「\(label)」的设备操作")
            .accessibilityIdentifier("device-actions-\(deviceId)")
        }
    }
}

struct PlaybackSessionCard: View {
    let session: API.ActivePlaybackSessionView
    let busy: Bool
    let onEnd: (String, String) -> Void
    let onRevoke: (String, String) -> Void

    var body: some View {
        let device = WatchFormat.deviceLabel(client: session.client, deviceName: session.deviceName)
        ActivityCardShell(percent: session.progressPercent, muted: session.paused) {
            ActivityPoster(media: session.media)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .top, spacing: 8) {
                    ActivityTitleText(media: session.media)
                    ActivityStatusDot(
                        color: session.paused ? Color.white.opacity(0.4) : Theme.success,
                        pulse: !session.paused, size: 8, label: session.paused ? "已暂停" : "播放中"
                    )
                    .padding(.top, 5)
                    DeviceActionsMenu(
                        deviceId: session.deviceId, label: device, onEnd: onEnd,
                        onRevoke: session.revocable ? onRevoke : nil, busy: busy
                    )
                    .padding(.top, -6)
                }
                Text(WatchFormat.metaLine([session.memberName, device, session.clientVersion]))
                    .font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                transferLine
                if let specs = specLine {
                    Text(specs).font(.caption2).foregroundStyle(Theme.textFaint.opacity(0.9)).lineLimit(1)
                }
                clockLine
            }
        }
        .accessibilityIdentifier("session-card")
    }

    private var transferLine: some View {
        HStack(spacing: 10) {
            if session.playMethod == "local" {
                if let rate = session.rateBytesPerSecond, rate > 0 {
                    Text(ActivityFormat.rate(rate)).fontWeight(.medium).foregroundStyle(Theme.info)
                } else {
                    Text("本地直连").foregroundStyle(Theme.textMuted)
                }
                if let sent = session.bytesSent, sent > 0 { Text("已传输 \(ActivityFormat.bytes(Double(sent)))") }
                if session.connections > 1 { Text("\(session.connections) 条连接") }
            } else {
                Text("网盘直链 · 流量不经过服务器")
            }
        }
        .font(.caption)
        .monospacedDigit()
        .foregroundStyle(Theme.textFaint)
    }

    /// 规格串（分辨率 · 编码 · HDR · 码率 · 体积）
    private var specLine: String? {
        guard let file = session.file else { return nil }
        let parts = [
            file.resolution, file.videoCodec?.uppercased(), file.hdr,
            file.bitRate.map { String(format: "%.1f Mbps", Double($0) / 1_000_000) },
            file.sizeBytes.map { ActivityFormat.bytes(Double($0)) },
        ].compactMap { $0 }.filter { !$0.isEmpty }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    @ViewBuilder private var clockLine: some View {
        if session.positionMs != nil || session.progressPercent != nil {
            HStack(spacing: 0) {
                if let position = session.positionMs {
                    Text(ActivityFormat.playClock(ms: position))
                    if let duration = session.durationMs {
                        Text(" / \(ActivityFormat.playClock(ms: duration))").foregroundStyle(Theme.textFaint.opacity(0.7))
                    }
                }
                if let percent = session.progressPercent {
                    if session.positionMs != nil { Text("  ·  ").foregroundStyle(Theme.textFaint.opacity(0.6)) }
                    Text("\(percent)%").fontWeight(.medium).foregroundStyle(Theme.textMuted)
                }
            }
            .font(.caption)
            .monospacedDigit()
            .foregroundStyle(Theme.textFaint)
            .padding(.top, 2)
        }
    }
}

struct FileDownloadCard: View {
    let download: API.ActiveFileDownloadView
    let busy: Bool
    let onRevoke: (String, String) -> Void

    var body: some View {
        let device = WatchFormat.deviceLabel(client: download.client, deviceName: download.deviceName)
        ActivityCardShell(percent: download.progressPercent) {
            ActivityPoster(media: download.media)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .top, spacing: 8) {
                    if let media = download.media {
                        ActivityTitleText(media: media)
                    } else {
                        Text(download.fileName).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.9))
                            .lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    DeviceActionsMenu(
                        deviceId: download.deviceId, label: device,
                        onRevoke: download.revocable ? onRevoke : nil, busy: busy
                    )
                    .padding(.top, -6)
                }
                Text(WatchFormat.metaLine([download.memberName, device]))
                    .font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                HStack(spacing: 10) {
                    if download.rateBytesPerSecond > 0 {
                        Text(ActivityFormat.rate(download.rateBytesPerSecond)).fontWeight(.medium).foregroundStyle(Theme.info)
                    }
                    HStack(spacing: 0) {
                        Text(ActivityFormat.bytes(Double(download.positionBytes)))
                        if download.sizeBytes > 0 {
                            Text(" / \(ActivityFormat.bytes(Double(download.sizeBytes)))").foregroundStyle(Theme.textFaint.opacity(0.7))
                        }
                        if let percent = download.progressPercent {
                            Text("  \(percent)%").fontWeight(.medium).foregroundStyle(Theme.textMuted)
                        }
                    }
                    if download.connections > 1 { Text("\(download.connections) 条连接") }
                }
                .font(.caption)
                .monospacedDigit()
                .foregroundStyle(Theme.textFaint)
                if download.media != nil {
                    Text(download.fileName).font(.caption).foregroundStyle(Theme.textFaint.opacity(0.8)).lineLimit(1).padding(.top, 2)
                }
            }
        }
    }
}

/// 范围外记录的折叠行：只报个数，不出片名；就地可切到「全部」
struct HiddenCountRow: View {
    let count: Int
    let noun: String
    var onShowAll: (() -> Void)?

    var body: some View {
        if count > 0 {
            HStack(spacing: 6) {
                Text("另有 \(count) \(noun)不在你的浏览范围内")
                if let onShowAll {
                    Text("·").foregroundStyle(Theme.textFaint.opacity(0.5))
                    Button("显示全部", action: onShowAll)
                        .fontWeight(.medium)
                        .foregroundStyle(Theme.info)
                        .buttonStyle(.plain)
                }
            }
            .font(.caption)
            .foregroundStyle(Theme.textFaint)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

extension View {
    /// 折叠行外面的淡卡片
    func activityHiddenRowCard() -> some View {
        background(Color.white.opacity(0.02), in: .rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.07)))
    }
}
