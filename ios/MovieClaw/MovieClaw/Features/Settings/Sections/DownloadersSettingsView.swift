import SwiftUI

/// 设置 → 下载器（对应 Web `downloader-config-section.tsx`）。
///
/// 与站点配置同构：清单展示已接入的下载器（qBittorrent / Transmission），
/// 保存后后端异步测试连接，有下载器处于 pending / verifying 时每 2 秒轮询，直到 active / failed。
///
/// 手机上的形态取舍：
/// - 每台下载器一个 Section：首行常驻名称 / 默认 / 连接状态 / 停用标，点整行展开详情（单开手风琴，同 Web）；
///   操作全收进右侧 ··· 菜单（启停 / 编辑 / 限速 / 默认 / 重测 / 删除），与 Web 菜单逐项一致；
/// - Web 把编辑表单嵌在展开详情里，手机屏幕窄，改成弹层（`SettingsBDlEditorSheet`）；
/// - 「限速与队列」同 Web 是独立弹层（`SettingsBDlLimitsSheet`）。
///
/// 所有 sheet / 轮询 / 首载都挂在 Form 根上：挂在 Section 上会被 List 分发到每一行，变成多个呈现者。
struct DownloadersSettingsView: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    /// 深链参数（Web 同名查询串）：`suggest_mapping` 体检修复卡的映射建议、`limits` 拥堵提示直达限速弹层
    @Environment(\.routeQuery) private var routeQuery
    /// 深链参数只消费一次（之后开合归用户，关了不复弹）
    @State private var routeQueryConsumed = false
    /// 体检修复卡带来的映射建议：页面级状态（同 Web），之后从菜单再点任何一台的「编辑配置」都预填，
    /// 而不是只有自动打开的那一次
    @State private var suggestMapping: String?

    @State private var downloaders: [API.DownloaderView] = []
    @State private var loading = true
    @State private var error: String?
    /// 当前展开详情的下载器（单开）
    @State private var expanded: Int?
    /// 编辑器：新建或编辑某台
    @State private var editor: SettingsBDlEditorTarget?
    /// 限速与队列弹层对应的下载器
    @State private var limitsTarget: SettingsBDlLimitsTarget?
    /// 正在执行菜单操作的下载器（执行期间禁用其菜单项，同 Web busy）
    @State private var busy: Set<Int> = []

    /// 需要轮询测试进度的中间态
    private var hasInProgress: Bool {
        downloaders.contains { SettingsBDlStatus.inProgress($0.status) }
    }

    var body: some View {
        Form {
            headerSection
            if loading {
                Section {
                    ProgressView().frame(maxWidth: .infinity).padding(.vertical, 12)
                }
            } else if downloaders.isEmpty {
                emptySection
            } else {
                ForEach(downloaders, id: \.id) { downloader in
                    SettingsBDlRowSection(
                        downloader: downloader,
                        expanded: expanded == downloader.id,
                        busy: busy.contains(downloader.id),
                        onToggle: {
                            withAnimation(.snappy) {
                                expanded = expanded == downloader.id ? nil : downloader.id
                            }
                        },
                        onAction: { action in Task { await perform(action, on: downloader) } }
                    )
                }
            }
        }
        .settingsBFormStyle()
        .task {
            await load()
            consumeRouteQuery()
        }
        // 有下载器处于中间态时 2 秒轮询；其余时间回调里直接跳过（Web 此时不轮询）
        .polling(every: hasInProgress ? 2 : 30) {
            guard hasInProgress else { return }
            if let rows = try? await api.dlList() { downloaders = rows }
        }
        .sheet(item: $editor) { target in
            SettingsBDlEditorSheet(downloader: target.downloader, suggestMapping: target.suggestMapping) { saved in
                upsert(saved)
            }
            .sheetFeedback()
        }
        .sheet(item: $limitsTarget) { target in
            SettingsBDlLimitsSheet(downloader: target.downloader)
                .sheetFeedback()
        }
    }

    // MARK: - 顶部说明 + 刷新 / 添加

    private var headerSection: some View {
        Section {
            if let error {
                SettingsBNotice(text: error, tone: .danger)
                    .accessibilityIdentifier("downloaders-error")
            }
            SettingsBIntro(text: summary)
        } header: {
            HStack(spacing: 8) {
                Spacer()
                Button("刷新") { Task { await load() } }
                    .font(.footnote.weight(.medium))
                    .buttonStyle(.glass)
                    .disabled(loading)
                    .accessibilityIdentifier("downloaders-refresh")
                Button {
                    editor = SettingsBDlEditorTarget(downloader: nil)
                } label: {
                    Label("添加下载器", systemImage: "plus").font(.footnote.weight(.semibold))
                }
                .discoverProminentButton()
                .disabled(loading)
                .accessibilityIdentifier("downloaders-add")
            }
            .textCase(nil)
        }
    }

    private var summary: String {
        if loading { return "加载中…" }
        if downloaders.isEmpty { return "接入你自己部署的下载软件，资源将由它们完成下载。" }
        let usable = downloaders.filter(\.usable).count
        return "已接入 \(downloaders.count) 个下载器，\(usable) 个可用。保存后系统会自动测试连接。"
    }

    private var emptySection: some View {
        Section {
            VStack(spacing: 10) {
                Image(systemName: "arrow.down.circle")
                    .font(.system(size: 30))
                    .foregroundStyle(Theme.textMuted)
                Text("还没有接入任何下载器").font(.body.weight(.medium))
                Text("点击右上角「添加下载器」，支持 qBittorrent 和 Transmission。")
                    .font(.footnote)
                    .foregroundStyle(Theme.textMuted)
                    .multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 20)
            .accessibilityIdentifier("downloaders-empty")
        }
    }

    // MARK: - 数据

    private func load() async {
        do {
            downloaders = try await api.dlList()
            error = nil
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }

    /// 落地后按深链参数自动打开对应弹层（Web：suggest_mapping 展开默认下载器并进编辑、预填映射；
    /// limits=<id> 打开那台的「限速与队列」）
    private func consumeRouteQuery() {
        guard !routeQueryConsumed, !downloaders.isEmpty else { return }
        routeQueryConsumed = true
        if let suggest = routeQuery["suggest_mapping"], !suggest.isEmpty {
            suggestMapping = suggest
            let target = downloaders.first(where: \.isDefault) ?? downloaders[0]
            expanded = target.id
            editor = SettingsBDlEditorTarget(downloader: target, suggestMapping: suggest)
        } else if let raw = routeQuery["limits"], let id = Int(raw), let target = downloaders.first(where: { $0.id == id }) {
            expanded = target.id
            limitsTarget = SettingsBDlLimitsTarget(downloader: target)
        }
    }

    /// 原地替换已有条目、新条目追加到末尾（保持列表顺序稳定，避免操作后跳位）
    private func upsert(_ next: API.DownloaderView) {
        if let idx = downloaders.firstIndex(where: { $0.id == next.id }) {
            downloaders[idx] = next
        } else {
            downloaders.append(next)
        }
    }

    private func perform(_ action: SettingsBDlAction, on downloader: API.DownloaderView) async {
        switch action {
        case .edit:
            editor = SettingsBDlEditorTarget(downloader: downloader, suggestMapping: suggestMapping)
            return
        case .limits:
            limitsTarget = SettingsBDlLimitsTarget(downloader: downloader)
            return
        case .delete:
            guard await feedback.confirm(
                "删除下载器「\(downloader.name)」？",
                message: "下载器中的任务不受影响，只是 movieclaw 不再向它投递。",
                confirmTitle: "删除",
                destructive: true
            ) else { return }
        case .toggleEnabled, .setDefault, .verify:
            break
        }
        busy.insert(downloader.id)
        defer { busy.remove(downloader.id) }
        error = nil
        do {
            switch action {
            case .toggleEnabled:
                upsert(try await api.dlStatusSet(downloaderId: downloader.id, body: .init(enabled: !downloader.enabled)))
            case .setDefault:
                _ = try await api.dlDefaultSet(downloaderId: downloader.id)
                // 原默认的标记同时被清掉，整体刷新一次拿到全量新状态
                await load()
            case .verify:
                upsert(try await api.dlVerify(downloaderId: downloader.id))
            case .delete:
                _ = try await api.dlDelete(downloaderId: downloader.id)
                downloaders.removeAll { $0.id == downloader.id }
                if expanded == downloader.id { expanded = nil }
                // 删除默认时后端会把默认让给另一台，整体刷新拿到新归属
                await load()
            case .edit, .limits:
                break
            }
        } catch is CancellationError {
        } catch {
            // Web 把失败原因放在页头红条；手机上页头可能已滚出视野，同时弹 Toast
            self.error = error.localizedDescription
            feedback.error(error)
        }
    }
}
