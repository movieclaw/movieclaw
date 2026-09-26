import SwiftUI

/// 活动总览「正在播放 / 正在下载」的行（系统分组列表里的一行），以及观看相关的共用小件。
///
/// 设备处置（结束播放 / 注销设备）不再挂在行尾的「⋯」上，而是走 iOS 惯用的左滑与长按菜单；
/// 两者都会先弹确认——打断别人正在看的东西、让电视重新登录，都不该一碰就生效。
@Observable
final class ActivityDeviceActions {
    /// 正在处置的设备（期间其它处置按钮禁用，防止连点）
    var busyDevice: String?

    /// 结束播放：可逆（再点播放即可继续），但会打断别人正在看的东西，问一句
    func end(deviceId: String, label: String, api: APIClient, feedback: Feedback, store: MediaActivityStore) {
        Task {
            let confirmed = await feedback.confirm(
                "结束「\(label)」本次播放？",
                message: "这台设备正在进行的播放会立刻中断，一分钟内不能续播；登录凭据与观看进度都不受影响，之后重新点播放即可继续。",
                confirmTitle: "结束播放"
            )
            guard confirmed else { return }
            await perform(deviceId, feedback: feedback, store: store, fallback: "结束播放失败") {
                try await api.activityEndPlayback(deviceId: deviceId)
            }
        }
    }

    /// 注销设备：设备要重新登录（电视上尤其麻烦），显式确认
    func revoke(deviceId: String, label: String, api: APIClient, feedback: Feedback, store: MediaActivityStore) {
        Task {
            let confirmed = await feedback.confirm(
                "注销这台设备？",
                message: "「\(label)」的登录凭据会立刻失效，正在进行的播放与下载一并停止，该设备下次使用需要重新登录。\n\n已看进度、收藏这些观看记录按账号保存，不会因为注销设备而丢失。",
                confirmTitle: "注销设备",
                destructive: true
            )
            guard confirmed else { return }
            await perform(deviceId, feedback: feedback, store: store, fallback: "注销设备失败") {
                try await api.activityRevokeDevice(deviceId: deviceId)
            }
        }
    }

    private func perform(_ deviceId: String, feedback: Feedback, store: MediaActivityStore, fallback: String,
                         _ call: () async throws -> String) async {
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
        try await activityDeviceAction("POST", ["playback", "activity", "sessions", deviceId, "end"], fallback: "已结束播放")
    }

    /// 注销一台播放设备
    func activityRevokeDevice(deviceId: String) async throws -> String {
        try await activityDeviceAction("DELETE", ["playback", "devices", deviceId], fallback: "设备已注销")
    }

    /// 设备 id 进路径前必须整段百分号编码（Web `encodeURIComponent(deviceId)`）：
    /// Jellyfin 网页客户端的 DeviceId 是 base64，可能带 `/`、`+`、`=`，原样拼进路径会被当成分隔符而 404。
    /// 通用的 `url(_:)` 走 `appending(path:)`，会把预先编码的 `%2F` 再编一次，所以这里自己拼已编码路径。
    private func activityDeviceAction(_ method: String, _ segments: [String], fallback: String) async throws -> String {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/?#;+=&"))
        let encoded = segments.map { $0.addingPercentEncoding(withAllowedCharacters: allowed) ?? $0 }
        guard var components = URLComponents(url: server.apiBase, resolvingAgainstBaseURL: false) else {
            throw APIError.network("服务器地址异常")
        }
        components.percentEncodedPath += "/" + encoded.joined(separator: "/")
        guard let url = components.url else { throw APIError.network("服务器地址异常") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let data = try await perform(request)
        let envelope = try? Self.decoder.decode(APIEnvelope<API.JSONValue?>.self, from: data)
        return envelope?.message ?? fallback
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
                .contentShape(.rect)
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

/// 正在播放的一行：海报 · 片名 / 成员 · 设备 · 客户端版本 / 播放状态与传输 / 进度条与时刻。
/// 左滑「结束播放」；长按菜单：打开影片详情、结束播放、注销此设备（仅持 Jellyfin 凭据的会话）。
struct ActivityPlaybackSessionRow: View {
    let session: API.ActivePlaybackSessionView
    let store: MediaActivityStore
    let actions: ActivityDeviceActions

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(Router.self) private var router

    var body: some View {
        let device = WatchFormat.deviceLabel(client: session.client, deviceName: session.deviceName)
        HStack(alignment: .top, spacing: 12) {
            ActivityPoster(media: session.media, width: 48, height: 70)
            VStack(alignment: .leading, spacing: 4) {
                ActivityTitleText(media: session.media)
                Text(WatchFormat.metaLine([session.memberName, device, session.clientVersion]))
                    .font(.footnote).foregroundStyle(Theme.textMuted).lineLimit(1)
                statusLine
                if let percent = session.progressPercent {
                    ProgressView(value: Double(min(100, max(0, percent))), total: 100)
                        .tint(session.paused ? Color.white.opacity(0.35) : Theme.info)
                        .padding(.top, 4)
                }
                clockLine
            }
        }
        .padding(.vertical, 4)
        .swipeActions(allowsFullSwipe: false) {
            Button("结束播放", systemImage: "stop.fill") { end(device) }
                .tint(.red)
                .disabled(actions.busyDevice != nil)
        }
        .contextMenu {
            if let route = WatchFormat.detailRoute(session.media) {
                Button("打开影片详情", systemImage: "film") { router.open(route) }
            }
            Button("结束播放", systemImage: "stop.circle") { end(device) }
            if session.revocable {
                Button("注销此设备", systemImage: "rectangle.portrait.and.arrow.right", role: .destructive) {
                    actions.revoke(deviceId: session.deviceId, label: device, api: api, feedback: feedback, store: store)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("session-card")
    }

    private func end(_ device: String) {
        actions.end(deviceId: session.deviceId, label: device, api: api, feedback: feedback, store: store)
    }

    /// 播放状态 + 传输：本地直连报实时速率与已传输量，网盘直链不经过服务器
    private var statusLine: some View {
        HStack(spacing: 8) {
            Label(session.paused ? "已暂停" : "播放中", systemImage: session.paused ? "pause.fill" : "play.fill")
                .labelStyle(.titleAndIcon)
                .foregroundStyle(session.paused ? Theme.textFaint : Theme.success)
            if session.playMethod == "local" {
                if let rate = session.rateBytesPerSecond, rate > 0 {
                    Text(ActivityFormat.rate(rate)).foregroundStyle(Theme.info)
                } else {
                    Text("本地直连")
                }
                if let sent = session.bytesSent, sent > 0 { Text("已传输 \(ActivityFormat.bytes(Double(sent)))") }
                if session.connections > 1 { Text("\(session.connections) 条连接") }
            } else {
                Text("网盘直链")
            }
        }
        .font(.footnote)
        .monospacedDigit()
        .lineLimit(1)
        .foregroundStyle(Theme.textFaint)
    }

    @ViewBuilder private var clockLine: some View {
        if let position = session.positionMs {
            HStack(spacing: 0) {
                Text(ActivityFormat.playClock(ms: position))
                if let duration = session.durationMs {
                    Text(" / \(ActivityFormat.playClock(ms: duration))").foregroundStyle(Theme.textFaint.opacity(0.7))
                }
                if let percent = session.progressPercent {
                    Text("  ·  \(percent)%").foregroundStyle(Theme.textMuted)
                }
            }
            .font(.caption)
            .monospacedDigit()
            .foregroundStyle(Theme.textFaint)
        }
    }
}

/// 正在下载（设备把片子下到本地）的一行；可注销设备的在左滑与长按里给出口
struct ActivityFileDownloadRow: View {
    let download: API.ActiveFileDownloadView
    let store: MediaActivityStore
    let actions: ActivityDeviceActions

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    var body: some View {
        let device = WatchFormat.deviceLabel(client: download.client, deviceName: download.deviceName)
        HStack(alignment: .top, spacing: 12) {
            ActivityPoster(media: download.media, width: 48, height: 70)
            VStack(alignment: .leading, spacing: 4) {
                if let media = download.media {
                    ActivityTitleText(media: media)
                } else {
                    Text(download.fileName).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.9)).lineLimit(1)
                }
                Text(WatchFormat.metaLine([download.memberName, device]))
                    .font(.footnote).foregroundStyle(Theme.textMuted).lineLimit(1)
                HStack(spacing: 8) {
                    if download.rateBytesPerSecond > 0 {
                        Text(ActivityFormat.rate(download.rateBytesPerSecond)).foregroundStyle(Theme.info)
                    }
                    Text(ActivityFormat.bytes(Double(download.positionBytes))
                        + (download.sizeBytes > 0 ? " / \(ActivityFormat.bytes(Double(download.sizeBytes)))" : ""))
                    if let percent = download.progressPercent { Text("\(percent)%").foregroundStyle(Theme.textMuted) }
                }
                .font(.footnote)
                .monospacedDigit()
                .lineLimit(1)
                .foregroundStyle(Theme.textFaint)
                if let percent = download.progressPercent {
                    ProgressView(value: Double(min(100, max(0, percent))), total: 100).tint(Theme.info).padding(.top, 4)
                }
                if download.media != nil {
                    Text(download.fileName).font(.caption).foregroundStyle(Theme.textFaint.opacity(0.8)).lineLimit(1)
                }
            }
        }
        .padding(.vertical, 4)
        .swipeActions(allowsFullSwipe: false) {
            if download.revocable {
                Button("注销设备", systemImage: "rectangle.portrait.and.arrow.right") { revoke(device) }
                    .tint(.red)
                    .disabled(actions.busyDevice != nil)
            }
        }
        .contextMenu {
            if download.revocable {
                Button("注销此设备", systemImage: "rectangle.portrait.and.arrow.right", role: .destructive) { revoke(device) }
            }
        }
    }

    private func revoke(_ device: String) {
        actions.revoke(deviceId: download.deviceId, label: device, api: api, feedback: feedback, store: store)
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
                    Button(action: onShowAll) {
                        Text("显示全部")
                            .fontWeight(.medium)
                            .foregroundStyle(Theme.info)
                            .expandedHitArea(vertical: 12)
                    }
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
