import SwiftUI

/// 站点资源结果页的操作状态：资源操作面板、下载目标弹窗、记忆确认条、看图灯箱，
/// 以及每一行的下载 / 投递状态（按行记，面板关掉再打开状态不丢）。
///
/// iOS 同一时刻只能展示一个弹层：从操作面板里点「下载」要先收起面板、再弹目标弹窗，
/// 这里统一用 `presentAfterDismiss` 串起来，调用方不用各自处理时序。
@Observable
final class TorrentActionsState {
    struct Confirming: Identifiable {
        var request: DownloadTargetRequest
        var target: API.DownloadTargetPrefView
        var id: String { request.id }
    }

    var sheetHit: TorrentHitItem?
    var dialogRequest: DownloadTargetRequest?
    var confirming: Confirming?
    var lightbox: DiscoverLightboxContent?
    var downloadStates: [String: TorrentSubmitState] = [:]
    var grabStates: [String: TorrentSubmitState] = [:]
    let prefs = DownloadTargetPrefs()
    /// 手动选种模式的目标订阅（结果页写入；灯箱里的「投给订阅」要用）
    var grabTarget: (id: Int, title: String)?

    /// 先收起当前弹层，等退场动画结束再展示下一个
    private func presentAfterDismiss(_ present: @escaping () -> Void) {
        let hadSheet = sheetHit != nil || confirming != nil || lightbox != nil
        sheetHit = nil
        confirming = nil
        lightbox = nil
        guard hadSheet else { present(); return }
        Task {
            try? await Task.sleep(for: .milliseconds(450))
            present()
        }
    }

    /// 点「下载」：没有记忆直接弹完整弹窗；有记忆先弹确认条；记忆失效时说明原因再弹完整弹窗
    func startDownload(_ hit: API.TorrentHit) {
        let state = downloadStates[hit.rowKey] ?? .idle
        guard state != .submitting, state != .done, state != .exists, var request = DownloadTargetRequest(hit: hit) else { return }
        guard let remembered = prefs.byCategory[request.category] else {
            presentAfterDismiss { self.dialogRequest = request }
            return
        }
        var reason: String?
        if remembered.kind == "smart", request.identity == nil {
            reason = "这条种子没解析出条目身份，用不了记住的「智能入库」，请手动选择。"
        } else if remembered.downloaderId != nil, remembered.downloaderName == nil {
            reason = "上次使用的下载器已不可用，请重新选择保存位置。"
        } else if prefs.isStaleDir(remembered) {
            reason = "上次的保存位置 \(remembered.savePath ?? "") 已不存在，请重新选择。"
        }
        if let reason {
            request.reason = reason
            presentAfterDismiss { self.dialogRequest = request }
        } else {
            presentAfterDismiss { self.confirming = Confirming(request: request, target: remembered) }
        }
    }

    /// 确认条「更改」/「不再记住」→ 完整弹窗
    func reopenDialog(_ request: DownloadTargetRequest, reason: String?) {
        var next = request
        next.reason = reason
        presentAfterDismiss { self.dialogRequest = next }
    }

    /// 确认条「确认下载」：按记忆直接提交；smart 未收敛时回落完整弹窗
    func confirmRemembered(_ confirming: Confirming, api: APIClient, feedback: Feedback) {
        let key = confirming.request.hitKey
        downloadStates[key] = .submitting
        self.confirming = nil
        Task {
            do {
                guard let result = try await api.submitRememberedDownload(confirming.request, target: confirming.target) else {
                    downloadStates[key] = .idle
                    reopenDialog(confirming.request, reason: "这条种子没匹配到唯一条目，请手动选择保存位置。")
                    return
                }
                downloadStates[key] = result.alreadyExists ? .exists : .done
                feedback.success(result.alreadyExists ? "该种子已在下载器中，未重复添加" : "已提交到「\(result.downloaderName)」\(result.savePath.map { " · \($0)" } ?? "")")
            } catch {
                downloadStates[key] = .error
                feedback.error(error.localizedDescription.isEmpty ? "提交失败，请重试" : error.localizedDescription)
            }
        }
    }

    /// 手动选种：直接投给订阅（跳过规则组过滤，身份匹配照常）
    func grab(_ hit: API.TorrentHit, target: (id: Int, title: String), api: APIClient, feedback: Feedback) {
        let state = grabStates[hit.rowKey] ?? .idle
        guard state != .submitting, state != .done else { return }
        grabStates[hit.rowKey] = .submitting
        var attrs: [String: API.JSONValue]?
        if let raw = hit.attrs, let data = try? JSONEncoder().encode(raw) {
            attrs = try? JSONDecoder().decode([String: API.JSONValue].self, from: data)
        }
        var payload = API.GrabPayload(siteId: hit.siteId, torrentId: hit.torrentId, title: hit.title)
        payload.subtitle = hit.subtitle
        payload.category = hit.category
        payload.attrs = attrs
        payload.downloadUrl = hit.downloadUrl
        payload.sizeBytes = hit.sizeBytes
        payload.seeders = hit.seeders
        payload.isFree = hit.free
        payload.hitAndRun = hit.hitAndRun
        payload.publishTime = hit.uploadTime
        Task {
            do {
                let result = try await api.subscriptionsDownloadSelectedTorrent(subscriptionId: target.id, body: payload)
                grabStates[hit.rowKey] = .done
                feedback.success("已投给《\(target.title)》，覆盖 \(result.units.count) 个追踪单元")
            } catch {
                grabStates[hit.rowKey] = .error
                feedback.error(error.localizedDescription.isEmpty ? "投递失败，请稍后重试" : error.localizedDescription)
            }
        }
    }

    /// 种子图集：海报 + 截图去重
    func slides(for hit: API.TorrentHit, api: APIClient) -> [String] {
        var seen = Set<String>()
        return ([hit.posterUrl].compactMap { $0 } + hit.imageUrls).filter { seen.insert($0).inserted }
    }

    /// 图览灯箱（同 Web 图览卡片的 ZoomLightbox）：三级地址——缩略条 photo-tile、舞台 photo-screen、
    /// 放大后才取图床原图；顶栏右侧放 详情 / 投给订阅 / 下载，看完截图当场就能下，不必退出灯箱再找
    func openImages(_ hit: API.TorrentHit, api: APIClient) {
        let urls = slides(for: hit, api: api)
        guard !urls.isEmpty else { return }
        let content = DiscoverLightboxContent(
            urls: urls.map { api.image($0, .photoScreen) },
            title: hit.title,
            brokenHint: "图床可能已失效或拒绝外链访问",
            thumbnails: urls.map { api.image($0, .photoTile) },
            originals: urls.map { api.image($0) },
            accessory: AnyView(TorrentLightboxActions(hit: hit, actions: self))
        )
        presentAfterDismiss { self.lightbox = content }
    }
}

/// 图览灯箱顶栏的操作键（同 Web 图览灯箱 actions：详情 / 投给订阅 / 下载，条件与操作面板一致）。
/// 点「下载」会先收起灯箱再弹保存位置弹窗或确认条（iOS 同一时刻只能展示一个弹层）。
struct TorrentLightboxActions: View {
    let hit: API.TorrentHit
    let actions: TorrentActionsState

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Feedback.self) private var feedback
    @Environment(\.openURL) private var openURL

    var body: some View {
        HStack(spacing: 6) {
            if let link = WebLink(hit.detailUrl) {
                Button("详情") { openURL(link.url) }
                    .buttonStyle(.glass)
                    .accessibilityIdentifier("lightbox-torrent-detail")
            }
            if let target = actions.grabTarget, hit.downloadUrl != nil {
                let state = actions.grabStates[hit.rowKey] ?? .idle
                Button(state.grabLabel) { actions.grab(hit, target: target, api: api, feedback: feedback) }
                    .buttonStyle(.glass)
                    .tint(Theme.info)
                    .disabled(state == .submitting || state == .done)
                    .accessibilityIdentifier("lightbox-torrent-grab")
            }
            if permissions.canDirectDownload, hit.downloadUrl != nil {
                let state = actions.downloadStates[hit.rowKey] ?? .idle
                Button(state.downloadLabel) { actions.startDownload(hit) }
                    .discoverProminentButton()
                    .disabled(state == .submitting || state == .done || state == .exists)
                    .accessibilityIdentifier("lightbox-torrent-download")
            }
        }
        .font(.subheadline.weight(.medium))
        .lineLimit(1)
    }
}

/// 资源操作面板（对应 Web TorrentActionsSheet）：片名 / 原始名 / 站点·体积·做种·时间，
/// 操作：浏览图片（仅图览卡片）、查看详情（站点详情页）、投给订阅（手动选种模式）、下载（需「一键下载」权限）。
struct TorrentActionsSheet: View {
    let hit: API.TorrentHit
    let actions: TorrentActionsState
    let grabTarget: (id: Int, title: String)?
    /// 从图览卡片打开时才给「浏览图片」（同 Web：只有图览卡片的抽屉传 onViewImages）
    var showsImages = false

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Feedback.self) private var feedback
    @Environment(\.openURL) private var openURL
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        let name = TorrentSearchLogic.parsedName(hit)
        let secondary = name != nil ? hit.title : (hit.subtitle.isEmpty ? nil : hit.subtitle)
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text(name?.primary ?? hit.title).font(.headline).foregroundStyle(Theme.text).lineLimit(2)
                if let secondary {
                    Text(secondary).font(.caption).foregroundStyle(Theme.textMuted).lineLimit(3)
                }
                Text([hit.siteName, TorrentSearchLogic.sizeText(hit), "\(hit.seeders) 做种", hit.uploadTime.map { SubsFormat.relative($0) }]
                    .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.caption)
                    .monospacedDigit()
                    .foregroundStyle(Theme.textFaint)
            }
            VStack(spacing: 10) {
                if showsImages, !actions.slides(for: hit, api: api).isEmpty {
                    actionButton("浏览图片", systemImage: "photo.on.rectangle", id: "torrent-action-images") {
                        actions.openImages(hit, api: api)
                    }
                }
                if let link = WebLink(hit.detailUrl) {
                    actionButton("查看详情", systemImage: "safari", id: "torrent-action-detail") {
                        dismiss()
                        openURL(link.url)
                    }
                }
                if let grabTarget, hit.downloadUrl != nil {
                    let state = actions.grabStates[hit.rowKey] ?? .idle
                    actionButton(state.grabLabel, systemImage: "tray.and.arrow.down", id: "torrent-action-grab", disabled: state == .submitting || state == .done) {
                        actions.grab(hit, target: grabTarget, api: api, feedback: feedback)
                    }
                }
                if permissions.canDirectDownload, hit.downloadUrl != nil {
                    let state = actions.downloadStates[hit.rowKey] ?? .idle
                    Button {
                        actions.startDownload(hit)
                    } label: {
                        Label(state.downloadLabel, systemImage: "arrow.down.circle")
                            .font(.body.weight(.semibold))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                    }
                    .discoverProminentButton()
                    .disabled(state == .submitting || state == .done || state == .exists)
                    .accessibilityIdentifier("torrent-action-download")
                }
            }
        }
        .padding(20)
        .presentationDetents([.medium])
        .presentationDragIndicator(.visible)
        .discoverContainer("torrent-actions")
    }

    private func actionButton(_ title: String, systemImage: String, id: String, disabled: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .font(.body.weight(.medium))
                .frame(maxWidth: .infinity)
                .padding(.vertical, 6)
        }
        .buttonStyle(.glass)
        .disabled(disabled)
        .accessibilityIdentifier(id)
    }
}

/// 逐站搜索详情：状态、命中条数、耗时（十几秒后失败多半是超时，秒失败多半是认证/解析）；
/// 失败站可「重试该站」或「去站点设置」；段脚给整次搜索总耗时。
/// 用 App 统一的玻璃弹层骨架（`SubsSheetScaffold`，同订阅弹层）：高度贴合内容、悬浮液态玻璃材质、左上 ✕ 关闭。
struct SiteStatusSheet: View {
    let model: TorrentSearchModel
    let canRetry: Bool
    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        SubsSheetScaffold(title: "站点搜索详情", closeTitle: "完成") {
            Section {
                ForEach(model.sites) { site in
                    row(site)
                }
            } footer: {
                if let total = model.totalElapsedMs {
                    Text("总耗时 \(TorrentSearchLogic.elapsedText(total))（以最慢的站点为准）")
                }
            }
        }
        .discoverContainer("site-status-sheet")
    }

    /// 一个站点：状态小圆点 + 站名，右边条数与耗时（搜索中转圈）；失败时下面写原因和两个补救动作
    private func row(_ site: TorrentSearchModel.SiteProgress) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Circle()
                    .fill(site.state == .searching ? Theme.accent : site.state == .error ? Theme.danger : Theme.success)
                    .frame(width: 7, height: 7)
                Text(site.siteName).foregroundStyle(Theme.text)
                Spacer()
                switch site.state {
                case .searching:
                    ProgressView().controlSize(.small).accessibilityLabel("搜索中")
                case .ok:
                    Text("\(site.count) 条" + (site.elapsedMs.map { " · \(TorrentSearchLogic.elapsedText($0))" } ?? ""))
                        .foregroundStyle(Theme.textMuted)
                case .error:
                    Text("失败" + (site.elapsedMs.map { " · \(TorrentSearchLogic.elapsedText($0))" } ?? ""))
                        .foregroundStyle(Theme.danger)
                }
            }
            .monospacedDigit()
            if site.state == .error {
                if let error = site.error {
                    Text(error)
                        .font(.subheadline)
                        .foregroundStyle(Theme.danger.opacity(0.85))
                        .padding(.leading, 17)
                }
                HStack(spacing: 8) {
                    if canRetry {
                        Button("重试该站", systemImage: "arrow.clockwise") { model.retrySite(site.siteId, api: api) }
                    }
                    Button("去站点设置", systemImage: "gearshape") {
                        dismiss()
                        router.push(.settingsSection(.sites))
                    }
                }
                .font(.subheadline)
                .buttonStyle(.glass)
                .controlSize(.small)
                .padding(.leading, 17)
            }
        }
        .padding(.vertical, 2)
    }
}
