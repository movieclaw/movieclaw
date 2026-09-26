import SwiftUI

/// 更新与维护 →「版本与更新」页签（Web app-update-section.tsx，机制见 docs/design/in-app-update.md）。
///
/// - 状态区：当前版本 + 代码来源；曾启动失败被自动回落的版本、未在运行的已装版本、异常退出横幅（可「知道了」）；
/// - **进页即知**：挂载时读服务端待更新快照（定时检查早就查过），有新版直接摆出版本卡片；
///   「检查更新」保留为「我现在就要重查一次」，手动检查的结论优先于快照；
/// - 更新执行：`POST /app/update/apply` 后 1 秒轮询进度；进入 restarting（或连续 3 次拉不到进度）后
///   改为轮询 `/health` 等服务恢复——先见到不可达、再见到恢复才算一次真实重启，恢复后重新拉取本页数据；
/// - NER 模型独立检查 / 更新；回退选择器（数据兼容三档：直接切换 / 恢复备份 / 无备份）；本地保留版本数 2~20；
/// - 维护：重启应用（二次确认），与更新共用同一套等待流程。
struct AppUpdatePanel<Header: View>: View {
    /// 分区顶部的页签条（由宿主传入，放进本面板自己的列表第一行）
    @ViewBuilder let header: () -> Header
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    enum RestartWait { case idle, waiting, timeout }
    enum RestartKind { case version, app }

    /// 新版本卡片的数据：手动检查结果与服务端快照归一到同一形状
    struct AvailableUpdate {
        var version: String
        var compatible: Bool
        var changelog: String
        /// 该版本曾在本机连续启动失败被回落（只有手动检查结果带这一位）
        var knownBad: Bool
    }

    @State private var status: Loadable<API.UpdateStatusView> = .loading
    @State private var check: API.UpdateCheckView?
    @State private var checking = false
    @State private var checkError: String?
    @State private var progress: API.UpdateProgressView?
    @State private var actionError: String?
    @State private var modelCheck: API.ModelUpdateCheckView?
    @State private var modelChecking = false
    @State private var modelError: String?
    @State private var pendingVersion: AvailableUpdate?
    @State private var pendingModelTag: String?
    @State private var restartWait: RestartWait = .idle
    @State private var restartKind: RestartKind = .version
    @State private var rollback: API.RollbackOptionsView?
    @State private var rollbackOpen = false
    @State private var retentionBusy = false
    @State private var pollTask: Task<Void, Never>?
    /// 回退 / 重启 / 进页恢复「重启中」的等待任务：离开页面即取消，不再在后台继续探测 /health（Web unmounted 守卫）
    @State private var restartTask: Task<Void, Never>?
    @State private var restartInfoShown = false

    var body: some View {
        List {
            Section { header() }
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 0, leading: 4, bottom: 0, trailing: 4))
            if restartWait != .idle {
                restartWaitingView
            } else {
                switch status {
                case .loading:
                    Section { SettingsLoadingRow(text: "正在加载版本信息…") }
                case .failed:
                    Section {
                        HStack {
                            Text("版本信息加载失败").foregroundStyle(Theme.textMuted)
                            Spacer()
                            Button("重试") { Task { await reloadStatus() } }.buttonStyle(.glass)
                        }
                    }
                case let .loaded(status):
                    content(status)
                }
            }
        }
        .task { await initialLoad() }
        .onDisappear {
            pollTask?.cancel()
            restartTask?.cancel()
        }
        .sheet(isPresented: $rollbackOpen) {
            RollbackSheet(targets: rollback?.targets ?? []) { target in
                rollbackOpen = false
                restartTask?.cancel()
                restartTask = Task { await doRollback(target) }
            }
            .sheetFeedback()
        }
    }

    // MARK: 加载

    private func reloadStatus() async {
        await Loadable.load(into: $status) { try await api.appUpdateStatus() }
    }

    private func initialLoad() async {
        await reloadStatus()
        // 恢复进行中的更新跟踪：中途进入本页时后台线程可能正在下载安装
        if let current = try? await api.appUpdateProgress() {
            if ["checking", "downloading", "verifying", "applying"].contains(current.phase) {
                progress = current
                startPollingProgress()
            } else if current.phase == "restarting" {
                progress = current
                restartTask?.cancel()
                restartTask = Task { await waitForRestart(.version) }
            } else if current.phase == "failed", current.error != nil {
                progress = current
            }
        }
        // 回退候选与保留策略（纯本地读盘，大目录统计较慢）与待更新快照并行拉取
        async let rollbackOptions = try? api.appUpdateRollbackOptions()
        // 进页预填：定时检查留下的快照（读库不触网）；读不到就退回「手动点检查更新」
        if let pending = try? await api.appUpdatePending() {
            pendingVersion = pending.appVersion.map {
                AvailableUpdate(version: $0, compatible: pending.appCompatible, changelog: pending.appChangelog, knownBad: false)
            }
            pendingModelTag = pending.modelTag
        }
        rollback = await rollbackOptions
    }

    // MARK: 进度与重启等待

    private func startPollingProgress() {
        pollTask?.cancel()
        pollTask = Task {
            var failStreak = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                if Task.isCancelled { return }
                do {
                    let current = try await api.appUpdateProgress()
                    failStreak = 0
                    progress = current
                    if current.phase == "failed" { return }
                    if current.phase == "restarting" {
                        await waitForRestart(.version)
                        return
                    }
                    if current.phase == "idle" {
                        // 活跃阶段之后读到 idle = 后端已重启完毕（新进程进度为空）：更新已生效
                        await finishRestart()
                        return
                    }
                } catch is CancellationError {
                    return
                } catch {
                    // 连续 3 次失败才认定「后端进入重启」，单次瞬时错误（反代 502、网络抖动）继续轮询
                    failStreak += 1
                    if failStreak >= 3 {
                        await waitForRestart(.version)
                        return
                    }
                }
            }
        }
    }

    /// 先观察到服务不可达（sawDown）、再观察到恢复才算一次真实重启；
    /// 等了约 60 秒都没见到不可达也放行（重启在两次探测间隙内完成）
    private func waitForRestart(_ kind: RestartKind) async {
        restartKind = kind
        restartWait = .waiting
        var sawDown = false
        for attempt in 0 ..< 90 {
            if Task.isCancelled { return }
            do {
                _ = try await api.health()
                if sawDown || attempt >= 30 {
                    await finishRestart()
                    return
                }
            } catch is CancellationError {
                return
            } catch {
                sawDown = true
            }
            try? await Task.sleep(for: .seconds(2))
        }
        restartWait = .timeout
    }

    /// 超时页「刷新页面」：Web 整页刷新（服务没起来就是一张打不开的页）。App 先探一次 /health，
    /// 通了才按恢复处理并提示；还没通就留在超时页如实告知，不再无条件报「应用已恢复」
    private func retryAfterTimeout() async {
        guard (try? await api.health()) != nil else {
            feedback.error("应用仍未恢复，请稍后再试")
            return
        }
        await finishRestart()
    }

    /// 服务恢复：Web 整页刷新，App 重新拉取本页全部数据
    private func finishRestart() async {
        restartWait = .idle
        progress = nil
        check = nil
        modelCheck = nil
        await initialLoad()
        feedback.success("应用已恢复")
    }

    private static var restartCopy: [RestartKind: [RestartWait: (String, String)]] {
        [
        .version: [
            .waiting: ("正在重启并切换版本…", "前后端会一起重启，服务恢复后页面自动刷新，通常需要几十秒。"),
            .timeout: ("等待超时，应用尚未恢复", "请稍后手动刷新页面。若反复无法恢复，容器会自动回落到更新前的版本，数据不受影响。"),
        ],
        .app: [
            .waiting: ("正在重启应用…", "服务恢复后页面会自动刷新，通常需要几秒到几十秒。"),
            .timeout: ("等待超时，应用尚未恢复", "Docker 部署通常几秒内自动拉起，请稍后手动刷新页面；源码部署且无 systemd 等守护时，需要到服务器上手动启动。"),
        ],
        ]
    }

    /// 重启等待态：全区替换为状态页，避免服务不可用期间继续操作
    private var restartWaitingView: some View {
        let copy = Self.restartCopy[restartKind]?[restartWait] ?? ("", "")
        return Section {
            VStack(spacing: 10) {
                if restartWait == .waiting { ProgressView() }
                Text(copy.0).font(.body.weight(.medium))
                Text(copy.1).font(.subheadline).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
                if restartWait == .timeout {
                    Button("刷新页面") { Task { await retryAfterTimeout() } }.buttonStyle(.glass)
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 24)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("restart-waiting")
    }

    // MARK: 内容

    @ViewBuilder
    private func content(_ status: API.UpdateStatusView) -> some View {
        let updating = progress.map { !["idle", "failed"].contains($0.phase) } ?? false
        let available: AvailableUpdate? = check.map {
            $0.updateAvailable
                ? AvailableUpdate(version: $0.latestVersion, compatible: $0.compatible, changelog: $0.changelog, knownBad: $0.latestKnownBad)
                : nil
        } ?? pendingVersion
        let availableModelTag: String? = modelCheck.map { $0.updateAvailable && $0.installable ? $0.latestTag : nil } ?? pendingModelTag
        let sourceLabel = status.codeSource == "overlay"
            ? "应用内更新版本\(status.overlayVersion.map { " v\($0)" } ?? "")"
            : status.codeSource == "baseline" ? "Docker 镜像内置" : "源码部署"

        Section("版本") {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .center, spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("当前版本").font(.body.weight(.medium))
                        Text("来源：\(sourceLabel)").font(.caption).foregroundStyle(Theme.textMuted)
                    }
                    Spacer(minLength: 6)
                    Text("v\(status.currentVersion)").font(.body.monospaced())
                        .lineLimit(1).minimumScaleFactor(0.7)
                        .accessibilityIdentifier("app-current-version")
                }
                if status.canUpdate, !updating {
                    HStack {
                        Spacer()
                        Button { Task { await doCheck() } } label: {
                            Label(checking ? "正在检查…" : "检查更新", systemImage: "arrow.clockwise")
                        }
                        .buttonStyle(.glass).controlSize(.small)
                        .disabled(checking)
                        .accessibilityIdentifier("app-check-update")
                    }
                }
                if let check, !check.updateAvailable {
                    Text("已是最新版本（v\(check.currentVersion)）").font(.caption).foregroundStyle(Theme.success)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
                if let checkError {
                    Text(checkError).font(.caption).foregroundStyle(Theme.danger).frame(maxWidth: .infinity, alignment: .trailing)
                }
                if !status.canUpdate {
                    Text("仅 Docker 镜像部署支持应用内更新；源码部署请用 git pull 更新")
                        .font(.caption).foregroundStyle(Theme.textFaint).frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
            if let inactive = status.inactiveOverlayVersion {
                Text("已安装的 v\(inactive) 未在运行\(status.inactiveOverlayReason.map { "：\($0)" } ?? "")")
                    .font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            if !status.badVersions.isEmpty {
                Text("版本 \(status.badVersions.map { "v\($0)" }.joined(separator: "、")) 曾连续启动失败，已被自动回落保护。可在新版本发布后重新更新。")
                    .font(.subheadline).foregroundStyle(Theme.warning)
            }
            if let exit = status.lastAbnormalExit {
                VStack(alignment: .leading, spacing: 8) {
                    Text("应用曾于 \(SettingsTime.unix(exit.at)) 异常退出并被容器自动恢复：\(exit.detail)（exit=\(exit.exitCode)）。若频繁出现，请查看容器日志排查。")
                        .font(.subheadline).foregroundStyle(Theme.warning)
                    HStack {
                        Spacer()
                        Button("知道了") { Task { await dismissExit() } }
                            .buttonStyle(.glass).controlSize(.small)
                            .accessibilityIdentifier("app-dismiss-exit")
                    }
                }
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("app-abnormal-exit")
            }
        }

        if updating, let progress {
            Section {
                VStack(alignment: .leading, spacing: 8) {
                    Text(progress.detail.isEmpty ? "正在更新…" : progress.detail).font(.body.weight(.medium))
                    if progress.phase == "downloading", let percent = progress.percent {
                        ProgressView(value: min(max(percent, 0), 100), total: 100).tint(Theme.accent)
                    }
                    Text("更新在后台执行，完成后会自动重启并刷新页面。").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
            }
        }

        if !updating, let available {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text("发现新版本 v\(available.version)").font(.body.weight(.medium))
                        Spacer()
                        if available.compatible {
                            Button("立即更新") { Task { await doApply() } }
                                .settingsProminentButton().controlSize(.small)
                                .accessibilityIdentifier("app-apply-update")
                        }
                    }
                    if !available.compatible {
                        Text("本次更新包含依赖变化，需拉取新的 Docker 镜像升级").font(.subheadline).foregroundStyle(Theme.warning)
                    }
                    if available.knownBad {
                        Text("注意：v\(available.version) 此前曾在本机连续启动失败被自动回落。重新更新会清除失败标记再试一次；若问题依旧，容器会再次自动回落，建议等待修复版本。")
                            .font(.subheadline).foregroundStyle(Theme.warning)
                    }
                    if !available.changelog.isEmpty {
                        ScrollView { SettingsMarkdownText(text: available.changelog) }
                            .frame(maxHeight: 256)
                    }
                }
            }
        }
        if let actionError {
            Section { SettingsNotice(text: actionError) }
        }

        // 上一次更新（应用或模型）异步失败的统一外显
        if let progress, progress.phase == "failed", let error = progress.error {
            Section {
                Text("上次更新\(progress.targetVersion.map { "（\($0)）" } ?? "")失败：\(error)")
                    .font(.subheadline).foregroundStyle(Theme.danger)
            }
        }

        if status.canUpdate {
            Section("NER 识别模型") {
                VStack(alignment: .leading, spacing: 6) {
                    Text("当前模型：\(status.modelTag ?? "无法识别（较早的镜像）")").font(.body.weight(.medium))
                    Text("种子名识别（NER）模型独立更新，无需升级镜像；更新后应用会自动重启并刷新页面。")
                        .font(.caption).foregroundStyle(Theme.textMuted)
                    if !updating {
                        HStack {
                            Spacer()
                            Button(modelChecking ? "正在检查…" : "检查模型更新") { Task { await doModelCheck() } }
                                .buttonStyle(.glass).controlSize(.small)
                                .disabled(modelChecking)
                                .accessibilityIdentifier("app-check-model")
                        }
                    }
                }
                if let availableModelTag {
                    HStack {
                        Text("发现新模型 \(availableModelTag)").font(.subheadline)
                        Spacer()
                        Button("更新模型") { Task { await doModelApply() } }
                            .settingsProminentButton().controlSize(.small)
                            .disabled(updating)
                    }
                }
                if let modelCheck, availableModelTag == nil {
                    Text(modelCheck.updateAvailable
                         ? "发现新模型 \(modelCheck.latestTag)，但该发布未携带更新清单，暂无法应用内安装"
                         : "模型已是最新（\(modelCheck.latestTag)）")
                        .font(.subheadline)
                        .foregroundStyle(modelCheck.updateAvailable ? Theme.warning : Theme.success)
                }
                if let modelError {
                    Text(modelError).font(.subheadline).foregroundStyle(Theme.danger)
                }
            }
        }

        if status.canUpdate, let rollback {
            Section("回退") {
                if !rollback.targets.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("更新后遇到问题时，可切换到本机保留的历史版本；跨数据库升级的回退会恢复对应时点的自动备份。")
                            .font(.subheadline).foregroundStyle(Theme.textMuted)
                        HStack {
                            Spacer()
                            Button("选择版本回退…") { rollbackOpen = true }
                                .buttonStyle(.glass).controlSize(.small)
                                .disabled(updating)
                                .accessibilityIdentifier("app-open-rollback")
                        }
                    }
                }
                HStack(spacing: 10) {
                    SettingsRowText(
                        title: "本地保留版本数",
                        detail: "保留越多可回退的范围越大，占用磁盘越多" + (rollback.versionsDirBytes > 0 ? "（当前占用 \(Formatters.bytes(rollback.versionsDirBytes))）" : ""),
                        detailColor: Theme.textMuted
                    )
                    Spacer(minLength: 6)
                    Button { Task { await setRetention(rollback.keepVersions - 1) } } label: { Image(systemName: "minus").frame(width: 22, height: 22) }
                        .buttonStyle(.glass)
                        .disabled(retentionBusy || rollback.keepVersions <= 2)
                        .accessibilityLabel("减少保留版本数")
                        .accessibilityIdentifier("app-retention-minus")
                    Text("\(rollback.keepVersions)").font(.body.weight(.medium)).monospacedDigit().frame(minWidth: 24)
                        .accessibilityIdentifier("app-retention-value")
                    Button { Task { await setRetention(rollback.keepVersions + 1) } } label: { Image(systemName: "plus").frame(width: 22, height: 22) }
                        .buttonStyle(.glass)
                        .disabled(retentionBusy || rollback.keepVersions >= 20)
                        .accessibilityLabel("增加保留版本数")
                        .accessibilityIdentifier("app-retention-plus")
                }
            }
        }

        Section("维护") {
            HStack(spacing: 8) {
                Text("重启应用").font(.body.weight(.medium))
                SettingsHelpTip(
                    text: "优雅停机后重新启动后端服务，正在进行的任务会中断。\n\nDocker 部署由容器入口自动拉起新进程，通常几秒内恢复；源码部署需有 systemd 等守护，否则退出后要到服务器上手动启动。",
                    label: "重启应用的说明"
                )
                Spacer()
                Button("重启应用") {
                    restartTask?.cancel()
                    restartTask = Task { await doRestart() }
                }
                    .buttonStyle(.glass)
                    .tint(Theme.danger)
                    .disabled(updating)
                    .accessibilityIdentifier("app-restart")
            }
        }
    }

    // MARK: 动作

    private func dismissExit() async {
        do {
            try await api.appUpdateLastExitDismiss()
            if case var .loaded(value) = status {
                value.lastAbnormalExit = nil
                status = .loaded(value)
            }
        } catch {
            actionError = "确认告警失败，请稍后重试"
        }
    }

    private func doCheck() async {
        checking = true
        checkError = nil
        check = nil
        defer { checking = false }
        do { check = try await api.appUpdateCheck() } catch { checkError = error.localizedDescription }
    }

    private func doApply() async {
        actionError = nil
        do {
            progress = try await api.appUpdateApply()
            startPollingProgress()
        } catch {
            actionError = error.localizedDescription
        }
    }

    private func doModelCheck() async {
        modelChecking = true
        modelError = nil
        modelCheck = nil
        defer { modelChecking = false }
        do { modelCheck = try await api.appUpdateModelCheck() } catch { modelError = error.localizedDescription }
    }

    private func doModelApply() async {
        modelError = nil
        do {
            progress = try await api.appUpdateModelApply()
            startPollingProgress()
        } catch {
            modelError = error.localizedDescription
        }
    }

    /// 执行回退：restore 由目标的数据兼容判定决定
    private func doRollback(_ target: API.RollbackTargetView) async {
        actionError = nil
        do {
            try await api.appUpdateRollback(body: .init(
                target: target.kind == "baseline" ? "baseline" : (target.version ?? ""),
                restoreBackup: target.schemaAction == "restore"
            ))
            await waitForRestart(.version)
        } catch {
            actionError = error.localizedDescription
        }
    }

    /// 重启应用：请求后端优雅停机，随后走与更新同一套等待流程（请求可能因进程退出而中断，属预期）
    private func doRestart() async {
        let ok = await feedback.confirm(
            "重启应用？",
            message: "重启期间服务短暂不可用，正在进行的下载投递/整理任务会中断。Docker 部署通常几秒内自动拉起；源码部署需有 systemd 等守护。",
            confirmTitle: "确认重启",
            destructive: true
        )
        guard ok else { return }
        Task { try? await api.appRestart() }
        await waitForRestart(.app)
    }

    /// 调整本地保留版本数（立即生效并按新策略清理），清理后占用与候选都可能变化，重拉一次
    private func setRetention(_ value: Int) async {
        guard let rollback, !retentionBusy else { return }
        let next = min(20, max(2, value))
        guard next != rollback.keepVersions else { return }
        retentionBusy = true
        defer { retentionBusy = false }
        do {
            try await api.appUpdateRetention(body: .init(keepVersions: next))
            self.rollback = try await api.appUpdateRollbackOptions()
        } catch {
            actionError = error.localizedDescription
        }
    }
}

// MARK: - 回退选择器

/// 候选版本（新的在前）→ 选中展开详情（数据后果 + 更新说明）→ 底部写明落点与数据后果的确认按钮
private struct RollbackSheet: View {
    let targets: [API.RollbackTargetView]
    let onConfirm: (API.RollbackTargetView) -> Void
    @State private var selected: Int?

    private func label(_ target: API.RollbackTargetView) -> String {
        target.kind == "baseline" ? "镜像内置版本\(target.version.map { " v\($0)" } ?? "")" : "v\(target.version ?? "")"
    }

    var body: some View {
        let pick = selected.map { targets[$0] }
        SettingsSheetScaffold(title: "选择回退版本") {
            Section {
                // 说明在标题下方、列表之前（同 Web 回退弹窗的头部说明）
                Text("回退会重启应用；是否需要恢复数据备份取决于目标版本的数据结构差异，结论已在每一项里标明。")
                    .font(.subheadline).foregroundStyle(Theme.textMuted)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets(top: 0, leading: 4, bottom: 0, trailing: 4))
            }
            Section {
                ForEach(Array(targets.enumerated()), id: \.offset) { index, target in
                    VStack(alignment: .leading, spacing: 8) {
                        Button {
                            withAnimation { selected = selected == index ? nil : index }
                        } label: {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(label(target)).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                                    schemaBadge(target)
                                    let meta = [target.installedAt.map { "装于 \(Formatters.dateTime($0))" },
                                                target.sizeBytes.map { Formatters.bytes($0) }].compactMap { $0 }
                                    if !meta.isEmpty {
                                        Text(meta.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.textFaint)
                                    }
                                }
                                Spacer()
                                Image(systemName: selected == index ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(selected == index ? Theme.accentStrong : Theme.textFaint)
                            }
                            .contentShape(.rect)
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("rollback-target-\(index)")
                        if selected == index {
                            Text(consequence(target)).font(.subheadline).foregroundStyle(color(target))
                            if let changelog = target.changelog, !changelog.isEmpty {
                                ScrollView { SettingsMarkdownText(text: changelog) }.frame(maxHeight: 192)
                            }
                        }
                    }
                }
            }
            Section {
                Button(role: .destructive) {
                    if let pick { onConfirm(pick) }
                } label: {
                    Text(pick.map { $0.schemaAction == "restore" ? "回退到 \(label($0)) 并恢复备份" : "回退到 \(label($0)) 并重启" } ?? "选择一个版本")
                        .frame(maxWidth: .infinity)
                }
                .disabled(pick == nil)
                .accessibilityIdentifier("rollback-confirm")
            }
        }
    }

    @ViewBuilder
    private func schemaBadge(_ target: API.RollbackTargetView) -> some View {
        switch target.schemaAction {
        case "switch": Text("可直接切换，数据保留").font(.caption).foregroundStyle(Theme.success)
        case "restore": Text("需恢复 \(Formatters.dateTime(target.backupTakenAt)) 的数据备份").font(.caption).foregroundStyle(Theme.warning)
        default: Text("无对应数据备份").font(.caption).foregroundStyle(Theme.danger)
        }
    }

    private func consequence(_ target: API.RollbackTargetView) -> String {
        switch target.schemaAction {
        case "switch": "该版本与当前的数据结构一致：直接切换代码，全部数据原样保留。"
        case "restore": "当前数据结构比该版本新（中间经过数据库升级）。回退将恢复 \(Formatters.dateTime(target.backupTakenAt)) 自动备份的数据，此后产生的数据（订阅活动、入库记录等）将丢失；回退前会再自动备份一次当前数据，切回新版本时可完整还原。"
        default: "没有可对应时点的数据备份：切换后该版本将直接使用当前数据，若中间经过数据库升级可能无法正常运行（启动失败时会自动回落）。"
        }
    }

    private func color(_ target: API.RollbackTargetView) -> Color {
        switch target.schemaAction {
        case "switch": Theme.success
        case "restore": Theme.warning
        default: Theme.danger
        }
    }
}

// MARK: - 更新说明

/// 更新说明（新版本卡片与回退候选共用）
struct SettingsMarkdownText: View {
    let text: String

    /// 更新说明是 GitHub Release 的 Markdown 原文：与 Web 一样复用全站的 Markdown 渲染器（紧凑档），
    /// 代码块、表格、有序列表都按结构排版，不再退化成纯文本
    var body: some View {
        AgentMarkdownView(text: text, size: 14)
    }
}
