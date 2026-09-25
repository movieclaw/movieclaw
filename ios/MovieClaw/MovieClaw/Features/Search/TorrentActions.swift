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

    func openImages(_ hit: API.TorrentHit, api: APIClient) {
        let urls = slides(for: hit, api: api)
        guard !urls.isEmpty else { return }
        let content = DiscoverLightboxContent(
            urls: urls.map { api.image($0, .photoScreen) },
            title: hit.title,
            brokenHint: "图床可能已失效或拒绝外链访问"
        )
        presentAfterDismiss { self.lightbox = content }
    }
}

/// 资源操作面板（对应 Web TorrentActionsSheet）：片名 / 原始名 / 站点·体积·做种·时间，
/// 操作：浏览图片、查看详情（站点详情页）、投给订阅（手动选种模式）、下载（需「一键下载」权限）。
struct TorrentActionsSheet: View {
    let hit: API.TorrentHit
    let actions: TorrentActionsState
    let grabTarget: (id: Int, title: String)?

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
                Text([hit.siteName, TorrentSearchLogic.sizeText(hit), "\(hit.seeders) 做种", hit.uploadTime.map(Formatters.relative)]
                    .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.caption)
                    .monospacedDigit()
                    .foregroundStyle(Theme.textFaint)
            }
            VStack(spacing: 10) {
                if !actions.slides(for: hit, api: api).isEmpty {
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

/// 筛选弹层（对应 Web FilterSheet）：站点 / 年份 / 季 / 集 / 片源 / 流媒体平台 / 编码 / HDR / 音频 / 字幕 / 压制组，
/// 组内多选为「或」、组间为「且」，每个选项带「选中后会看到的条数」，底部实时显示命中数。
struct TorrentFilterSheet: View {
    @Bindable var model: TorrentSearchModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("可多选，分类之间组合生效").font(.caption).foregroundStyle(Theme.textFaint)
                    ForEach(TorrentFilterDim.sheetDims, id: \.self) { dim in
                        let values = dimValues(dim)
                        if !values.isEmpty {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(dim.title).font(.caption.weight(.medium)).foregroundStyle(Theme.textFaint)
                                DiscoverFlowLayout(spacing: 6, lineSpacing: 6) {
                                    ForEach(values, id: \.value) { facet in
                                        DiscoverChip(
                                            label: TorrentSearchLogic.facetLabel(dim, facet.value, siteName: model.siteName),
                                            count: facet.count,
                                            active: model.filters.values(dim).contains(facet.value)
                                        ) {
                                            model.filters.toggle(dim, facet.value)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                .padding(Theme.pagePadding)
            }
            .background(Theme.background)
            .navigationTitle("筛选结果")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("清除全部") { model.filters = TorrentFilters() }
                        .accessibilityIdentifier("torrent-filter-clear")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("查看 \(model.filtered.count) 条结果") { dismiss() }
                        .discoverProminentButton()
                        .accessibilityIdentifier("torrent-filter-done")
                }
            }
        }
        .presentationDetents([.large])
        .discoverContainer("torrent-filter-sheet")
    }

    /// 站点维度只列成功返回的站点（顺序同站点状态），其余维度用聚合结果
    private func dimValues(_ dim: TorrentFilterDim) -> [TorrentFacetValue] {
        guard dim == .site else { return model.facets.values(dim) }
        let counts = Dictionary(model.facets.values(.site).map { ($0.value, $0.count) }, uniquingKeysWith: { a, _ in a })
        return model.okSites.map { TorrentFacetValue(value: $0.siteId, count: counts[$0.siteId] ?? 0) }
    }
}

/// 逐站搜索详情：状态、命中条数、耗时（十几秒后失败多半是超时，秒失败多半是认证/解析）；
/// 失败站可「重试该站」或「去站点设置」；底部给整次搜索总耗时。
struct SiteStatusSheet: View {
    let model: TorrentSearchModel
    let canRetry: Bool
    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                ForEach(model.sites) { site in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(spacing: 8) {
                            Circle()
                                .fill(site.state == .searching ? Theme.accent : site.state == .error ? Theme.danger : Theme.success)
                                .frame(width: 7, height: 7)
                            Text(site.siteName).foregroundStyle(Theme.text)
                            Spacer()
                            switch site.state {
                            case .searching:
                                Text("搜索中…").foregroundStyle(Theme.textFaint)
                            case .ok:
                                Text("\(site.count) 条" + (site.elapsedMs.map { " · \(TorrentSearchLogic.elapsedText($0))" } ?? ""))
                                    .foregroundStyle(Theme.textMuted)
                            case .error:
                                Text("失败" + (site.elapsedMs.map { " · \(TorrentSearchLogic.elapsedText($0))" } ?? ""))
                                    .foregroundStyle(Theme.danger)
                            }
                        }
                        .font(.subheadline)
                        .monospacedDigit()
                        if site.state == .error {
                            if let error = site.error {
                                Text(error).font(.caption).foregroundStyle(Color(red: 1, green: 0.6, blue: 0.6).opacity(0.85))
                            }
                            HStack(spacing: 16) {
                                if canRetry {
                                    Button("重试该站") { model.retrySite(site.siteId, api: api) }
                                }
                                Button("去站点设置 ›") {
                                    dismiss()
                                    router.push(.settingsSection(.sites))
                                }
                            }
                            .font(.caption.weight(.semibold))
                            .buttonStyle(.borderless)
                        }
                    }
                }
                if let total = model.totalElapsedMs {
                    Text("总耗时 \(TorrentSearchLogic.elapsedText(total))（以最慢的站点为准）")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                }
            }
            .scrollContentBackground(.hidden)
            .background(Theme.background)
            .navigationTitle("站点搜索详情")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
        }
        .presentationDetents([.medium, .large])
        .discoverContainer("torrent-sites-sheet")
    }
}
