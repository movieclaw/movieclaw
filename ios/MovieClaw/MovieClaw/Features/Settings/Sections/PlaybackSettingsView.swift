import SwiftUI

/// 设置 → 播放（Web settings-view.tsx 的 PlaybackSection：进度条预览、转码缓存、远程转码）。
///
/// - 播放引擎（**App 专属**，存本机）：自动 / 系统播放器 / MPV，键名与播放器模块共用（见 `SettingsPlaybackEngine`）；
/// - 进度条预览、转码缓存：两颗开关改即存（`PUT /playback/policy`），失败回滚（乐观更新）；
/// - 远程转码：开关是**意图**、Worker 连接是**现实**，两件事分开说——
///   配置 `GET/PUT /transcode-worker/config`，在线状态 `GET /transcode-worker/status` 每 5 秒轮询，
///   同时轮询待批准请求与已授权 Worker（断线的 Worker 也要列出来，否则 Mac 一关机就「凭空消失」）；
///   保存才落库，「高级」里的覆盖地址只服务于反代改写 Host 的少数部署。
struct PlaybackSettingsView: View {
    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @AppStorage(SettingsPlaybackEngine.storageKey) private var engine = SettingsPlaybackEngine.auto.rawValue

    @State private var policy: API.PlaybackPolicyView?
    /// 策略错误及其归属：加载失败两张卡都显示；保存失败只显示在出错的那张卡（Web 两张卡各管各的错误）
    @State private var policyError: String?
    @State private var policyErrorScope: PolicyErrorScope = .load
    enum PolicyErrorScope { case load, trickplay, cache }
    @State private var policyBusy = false

    @State private var config: Loadable<API.RemoteTranscodeConfigView> = .loading
    @State private var enabled = false
    @State private var baseURLDraft = ""
    @State private var advancedOpen = false
    @State private var saving = false
    @State private var saveError: String?
    @State private var saved = false
    @State private var status: SettingsTranscodeWorkerStatus?
    @State private var pendingWorkers: [API.DeviceRequestView] = []
    @State private var authorizedWorkers: [API.ApiTokenView] = []

    /// 单个 HLS 产物上传上限固定 512 MiB（Worker 上传代理的实现上限，不是偏好，不给用户填）
    private static let artifactLimitBytes = 512 * 1024 * 1024

    var body: some View {
        List {
            engineSection
            policySection
            remoteSections
        }
        .scrollDismissesKeyboard(.interactively)
        .appBackground()
        .task { await loadPolicy() }
        .task { await loadConfig() }
        .polling(every: 5, immediately: true) { await pollStatus() }
    }

    // MARK: 播放引擎（本机）

    private var engineSection: some View {
        Section {
            Picker("播放引擎", selection: $engine) {
                ForEach(SettingsPlaybackEngine.allCases) { option in
                    Text(option.label).tag(option.rawValue)
                }
            }
            .pickerStyle(.segmented)
            .accessibilityIdentifier("playback-engine")
            Text((SettingsPlaybackEngine(rawValue: engine) ?? .auto).hint)
                .font(.caption).foregroundStyle(Theme.textFaint)
        } header: {
            Text("播放引擎（本机）")
        } footer: {
            Text("只影响这台设备上的 App；自动 = 服务端判定可直出且系统播放器支持时用系统播放器，否则交给 MPV，MPV 失败回落服务端转码。")
        }
    }

    // MARK: 进度条预览 / 转码缓存

    @ViewBuilder
    private var policySection: some View {
        Section("进度条预览") {
            if let policyError, policyErrorScope != .cache { SettingsNotice(text: policyError) }
            if let policy {
                Toggle(isOn: Binding(get: { policy.trickplayEnabled }, set: { value in Task { await savePolicy(trickplay: value) } })) {
                    SettingsRowText(
                        title: "生成进度条预览图",
                        detail: policy.trickplayEnabled
                            ? "拖动进度条时可以看到画面缩略图；影片入库后首次播放会在后台慢慢生成"
                            : "不再为新影片生成预览，已生成的照常显示；重新打开即恢复生成"
                    )
                }
                .disabled(policyBusy)
                .accessibilityIdentifier("playback-trickplay")
            } else if policyError == nil {
                SettingsLoadingRow()
            }
        }
        Section("转码缓存") {
            if let policyError, policyErrorScope != .trickplay { SettingsNotice(text: policyError) }
            if let policy {
                Toggle(isOn: Binding(get: { policy.transcodeCacheEnabled }, set: { value in Task { await savePolicy(cache: value) } })) {
                    SettingsRowText(
                        title: "保留转码产物供续播、重看复用",
                        detail: policy.transcodeCacheEnabled
                            ? "同一部片再次播放时已转出的部分直接读文件、不重新转码；缓存按磁盘剩余空间自动限额、24 小时未用自动清理，也可在「存储」页清空"
                            : "会话结束即删除分片，每次播放都重新转码；磁盘特别紧张时用"
                    )
                }
                .disabled(policyBusy)
                .accessibilityIdentifier("playback-transcode-cache")
            } else if policyError == nil {
                SettingsLoadingRow()
            }
        }
    }

    private func loadPolicy() async {
        do {
            policy = try await api.playbackPolicyShow()
            policyError = nil
        } catch {
            policyError = error.localizedDescription
            policyErrorScope = .load
        }
    }

    /// 改即存：先乐观更新，失败回滚并显示后端原因
    private func savePolicy(trickplay: Bool? = nil, cache: Bool? = nil) async {
        guard let previous = policy else { return }
        var optimistic = previous
        if let trickplay { optimistic.trickplayEnabled = trickplay }
        if let cache { optimistic.transcodeCacheEnabled = cache }
        policy = optimistic
        policyBusy = true
        policyError = nil
        defer { policyBusy = false }
        do {
            policy = try await api.playbackPolicySet(body: .init(trickplayEnabled: trickplay, transcodeCacheEnabled: cache))
        } catch {
            policy = previous
            policyError = error.localizedDescription
            policyErrorScope = trickplay != nil ? .trickplay : .cache
        }
    }

    // MARK: 远程转码

    @ViewBuilder
    private var remoteSections: some View {
        switch config {
        case .loading:
            Section("远程转码") { SettingsLoadingRow(text: "正在加载远程转码设置…") }
        case let .failed(message):
            Section("远程转码") {
                Text("远程转码设置加载失败").foregroundStyle(Theme.textMuted)
                Text(message).font(.footnote).foregroundStyle(Theme.danger)
                Button("重试") { Task { await loadConfig() } }
            }
        case let .loaded(config):
            remoteLoaded(config)
        }
    }

    @ViewBuilder
    private func remoteLoaded(_ config: API.RemoteTranscodeConfigView) -> some View {
        let workers = status?.workers ?? []
        let online = workers.filter(\.online)
        let liveNames = Set(workers.map(\.workerId))
        let offline = authorizedWorkers.filter { !liveNames.contains($0.name) }
        let hasAnyWorker = !workers.isEmpty || !offline.isEmpty
        let statusText = !config.enabled ? "已关闭"
            : !config.ready ? "「高级」里的覆盖地址不合法，暂不会分配远程转码任务"
            : !online.isEmpty ? "已就绪，\(online.count) 个 Worker 在线"
            : "配置已就绪，但还没有 Worker 连上来"
        let statusColor: Color = !config.enabled ? Theme.textMuted : (config.ready && !online.isEmpty ? Theme.success : Theme.warning)

        // 远程转码介绍放在最上面（Web 的段首说明），下面才是「状态」组
        Section {
            if let saveError { SettingsNotice(text: saveError) }
            Text("只把需要远程硬件能力的转码任务交给兼容 Worker。NAS 仍负责鉴权、播放会话和 HLS 缓存；修改后立即生效，不需要重启应用。当前可用的 Worker 实现为 macOS Apple Silicon 版本。")
                .font(.subheadline).foregroundStyle(Theme.textMuted)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 4, leading: 4, bottom: 4, trailing: 4))
        }

        Section {
            Toggle(isOn: $enabled) {
                SettingsRowText(
                    title: "启用远程硬件转码",
                    detail: enabled
                        ? "没有 Worker 在线时，播放自动回落到 NAS 本地转码，不会失败"
                        : "关闭后已配对的 Worker 也会断开连接，所有播放走 NAS 本地转码"
                )
            }
            .accessibilityIdentifier("remote-transcode-enabled")
            Text("当前状态：\(Text(statusText).foregroundStyle(statusColor))")
                .font(.subheadline)
            ForEach(config.issues, id: \.self) { issue in
                Text("· \(issue)").font(.caption).foregroundStyle(Theme.warning)
            }
        } header: {
            Text("状态")
        }

        Section("Worker") {
            if !pendingWorkers.isEmpty {
                HStack(spacing: 10) {
                    Text("有 \(pendingWorkers.count) 台 Worker 正在等待批准 \(Text(pendingWorkers.map(\.userCode).joined(separator: " · ")).font(.caption.monospaced()))")
                        .font(.subheadline).foregroundStyle(Theme.warning)
                    Spacer()
                    Button("去审批") { router.push(.settingsSection(.devices)) }
                        .buttonStyle(.glass).controlSize(.small)
                }
            }
            if !config.ready {
                SettingsNotice(
                    text: !config.enabled
                        ? "远程转码还没开启，Worker 现在连不上来。打开上面的开关并保存即可。"
                        : "「高级」里填的覆盖地址不合法，Worker 现在连不上来。改正或清空它即可。",
                    tone: .warn
                )
            }
            // 状态接口拿不到（含失败）时停在这句（Web status == null），不去猜引导或离线列表
            if status == nil {
                Text("正在获取 Worker 状态…").font(.caption).foregroundStyle(Theme.textFaint)
            } else if hasAnyWorker {
                ForEach(workers, id: \.workerId) { worker in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(worker.workerId).font(.subheadline.weight(.medium))
                            Spacer()
                            Text(worker.online ? (worker.draining ? "暂停接单" : "在线") : "已离线")
                                .font(.caption)
                                .foregroundStyle(worker.online ? (worker.draining ? Theme.warning : Theme.success) : Theme.textFaint)
                        }
                        Text(worker.summary).font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                ForEach(offline, id: \.id) { device in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(device.name).font(.subheadline.weight(.medium)).foregroundStyle(Theme.textMuted)
                            Spacer()
                            Text("未连接").font(.caption).foregroundStyle(Theme.textFaint)
                        }
                        Text("已授权 · 最近活跃 \(SettingsTime.deviceRelative(device.lastUsedAt))\(config.ready ? " · Mac 没开机或没联网时属正常" : "")")
                            .font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                Button("在「设备」里查看授权或吊销") { router.push(.settingsSection(.devices)) }
                    .font(.caption)
            } else if config.ready {
                VStack(alignment: .leading, spacing: 6) {
                    Text("还没有 Worker 接入。在 Mac 上：").font(.subheadline).foregroundStyle(Theme.textMuted)
                    Text("1. 打开 MovieClaw Transcoder，点「在局域网中查找」或直接填 movieclaw 地址；")
                    Text("2. 点「连接并配对」，它会显示一段配对码；")
                    Text("3. 回到「设置 → 设备」，核对配对码后批准。")
                    Text("全程不需要在任何一边输入令牌——Worker 的凭证是批准时签发的，直接回到那台 Mac，不经过屏幕。")
                }
                .font(.caption).foregroundStyle(Theme.textFaint)
                Button("去设备页") { router.push(.settingsSection(.devices)) }
                    .font(.caption)
            }
        }

        Section("高级") {
            DisclosureGroup(isExpanded: $advancedOpen) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("服务端下发任务时要告诉 Worker「去哪儿取源视频、往哪儿传 HLS 产物」。默认自动取用这台 Worker 连上来时用的地址，不需要设置——它刚从那儿握上手，必然够得着，而且通常就是最快的那条内网路径。只有当反向代理把 Host 改写成了上游地址（如 127.0.0.1:8000），导致 Worker 拿到的地址回不来时，才需要在这里指定一个 Worker 够得着的地址。")
                        .font(.caption).foregroundStyle(Theme.textFaint)
                    Text("覆盖地址").font(.subheadline.weight(.medium)).foregroundStyle(Theme.textMuted)
                    TextField("留空 = 自动（推荐）", text: $baseURLDraft)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .padding(10)
                        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 10))
                        .accessibilityIdentifier("remote-transcode-base-url")
                    Text(config.baseUrlSource == "remote_transcode_setting" ? "当前使用上面填写的覆盖地址。" : "当前为自动：每台 Worker 各用自己连上来的地址。")
                        .font(.caption).foregroundStyle(Theme.textFaint)
                }
                .padding(.top, 6)
            } label: {
                HStack(spacing: 8) {
                    Text("取源与回传地址")
                    Text(config.baseUrlSource == "worker_connection" ? "自动" : "已覆盖为 \(config.baseUrl)")
                        .font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                }
            }
        }

        Section {
            HStack {
                if saved {
                    Label("已保存", systemImage: "checkmark").font(.subheadline).foregroundStyle(Theme.success)
                }
                Spacer()
                Button(saving ? "保存中…" : "保存设置") { Task { await saveConfig() } }
                    .settingsProminentButton()
                    .disabled(saving)
                    .accessibilityIdentifier("remote-transcode-save")
            }
            .listRowBackground(Color.clear)
        }
    }

    private func loadConfig() async {
        await Loadable.load(into: $config) { try await api.transcodeConfigShow() }
        if let value = config.value {
            enabled = value.enabled
            baseURLDraft = value.baseUrlOverride
            advancedOpen = !value.baseUrl.isEmpty
        }
    }

    /// 在线状态与授权清单：附属指示器，失败不弹错，不盖掉用户正在填的表单
    private func pollStatus() async {
        status = try? await api.send("GET", "/transcode-worker/status", as: SettingsTranscodeWorkerStatus.self)
        do {
            async let requests = api.authDevicesRequests()
            async let devices = api.authTokensList()
            pendingWorkers = try await requests.filter { $0.clientType == "worker" }
            authorizedWorkers = try await devices.filter { $0.clientType == "worker" }
        } catch {
            pendingWorkers = []
            authorizedWorkers = []
        }
    }

    private func saveConfig() async {
        saving = true
        saveError = nil
        saved = false
        defer { saving = false }
        do {
            let next = try await api.transcodeConfigSet(body: .init(
                enabled: enabled,
                baseUrl: baseURLDraft.trimmingCharacters(in: .whitespaces),
                // 存量若被调低过，这一步顺手拉回默认值——高上限只会更少地误伤
                maxArtifactBytes: Self.artifactLimitBytes
            ))
            config = .loaded(next)
            enabled = next.enabled
            baseURLDraft = next.baseUrlOverride
            saved = true
            Task {
                try? await Task.sleep(for: .seconds(2.2))
                saved = false
            }
            await pollStatus()
        } catch {
            saveError = error.localizedDescription
        }
    }
}

/// `GET /transcode-worker/status` 的响应（生成器给的是任意字典，这里按 Web lib/api/transcode-worker.ts 手写）
nonisolated struct SettingsTranscodeWorkerStatus: Decodable, Sendable {
    struct Worker: Decodable, Sendable {
        let workerId: String
        let workerVersion: String?
        let arch: String?
        let platform: String?
        let ffmpegVersion: String?
        let backends: [String]
        let maxJobs: Int
        let activeJobs: Int
        let draining: Bool
        let lastSeenSeconds: Double
        let online: Bool

        enum CodingKeys: String, CodingKey {
            case workerId = "worker_id", workerVersion = "worker_version", arch, platform
            case ffmpegVersion = "ffmpeg_version", backends, maxJobs = "max_jobs", activeJobs = "active_jobs"
            case draining, lastSeenSeconds = "last_seen_seconds", online
        }

        /// 「macOS · arm64 · ffmpeg 7.1 · videotoolbox · 任务 0/2 · 3 秒前活跃」
        var summary: String {
            // 空串与 nil 一样滤掉（Web filter(Boolean)），不会出现「ffmpeg 」这种半截字段
            let ffmpeg = ffmpegVersion.flatMap { $0.isEmpty ? nil : "ffmpeg \($0)" }
            return [
                platform, arch, ffmpeg,
                backends.isEmpty ? nil : backends.joined(separator: "/"),
                "任务 \(activeJobs)/\(maxJobs)", "\(Int(lastSeenSeconds.rounded())) 秒前活跃",
            ].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        }
    }

    let enabled: Bool
    let ready: Bool
    let workers: [Worker]
}
