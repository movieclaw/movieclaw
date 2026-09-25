import AVFoundation
import SwiftUI

/// 播放控制器：会话协议 + 状态机 + 引擎编排（对应 Web `components/player/video-player.tsx` 与 `lib/player/machine.ts`）。
///
/// ## 起播链路
/// 「决策 → 开会话 → 挂引擎 → 缓冲 → 出画」四段异步，中间随时可能插进来 seek、换轨、降档、切集。
/// 为此每次「去后端要一个能播的地址」都带一个递增的 `attempt` 序号：响应回来时序号已被超越
/// （用户又换了参数/退出了），就把刚拉起的会话当场掐掉，绝不让它变成占着转码名额的孤儿。
///
/// ## 引擎选择（docs/design/ios-app.md §4）
/// - 系统播放器：一律 AVPlayer，吃不下的交给服务端转封装/转码；
/// - MPV：一律 libmpv，视频可直通（档 0–2）就直接拉原文件，转码档照样能放 HLS；
/// - 自动：先用 AVPlayer 的能力问一次决策（`/decide`，不起会话）——能原文件直出（档 0）就用 AVPlayer；
///   用户自己限了画质/线路不够导致的转码仍交给 AVPlayer 放 HLS；其余（MKV、TrueHD、需要转码的编码……）交给 MPV 直出。
/// - MPV 出任何问题（创建失败、放不了）都回落到服务端 HLS + AVPlayer，并在本单元内不再尝试 MPV。
///
/// ## 时间轴
/// 引擎只认「流时间」。文件时间 = `originMs` + 流时间：原文件直出与 VOD 播放列表（timeline=file）
/// 的参照点是 0；旧式会话相对列表（timeline=session）的参照点是会话起点 `start_ms`。
@MainActor
@Observable
final class PlaybackController {
    enum Phase: Equatable {
        case idle, deciding, sessionStarting, buffering, playing, degrading, consent, error, ended

        /// 转圈该不该显示：起播四段与降档重来都算「还没出画」
        var isBusy: Bool { [.deciding, .sessionStarting, .buffering, .degrading].contains(self) }

        /// 转圈时说清楚卡在哪一段（文案同 Web busyLabel）
        var busyLabel: String {
            switch self {
            case .deciding: "正在判断播放方式…"
            case .sessionStarting: "正在准备视频流…"
            case .degrading: "这一档放不出来，正在降档重试…"
            default: "正在缓冲…"
            }
        }
    }

    // MARK: 输入

    let request: PlayRequest
    let scope: PlaybackAPI

    // MARK: 条目与单元

    private(set) var info: API.PlaybackItemView?
    private(set) var episodes: [API.EpisodeView] = []
    private(set) var unit: PlaybackUnit
    private(set) var infoError: String?

    // MARK: 状态机

    private(set) var phase: Phase = .idle
    private(set) var session: API.PlaybackSessionView?
    /// 等用户拍板的决策（同意弹窗）
    private(set) var pendingDecision: API.PlaybackDecisionView?
    private(set) var errorMessage: String?
    private(set) var errorSuggestion: String?
    private var failedTiers: [Int] = []
    private var failureCount = 0
    private var attempt = 0
    private var consentGranted = false

    // MARK: 引擎

    private(set) var engine: (any PlayerEngine)?
    /// 当前会话在服务端的 id（MPV 直出原文件时会立即释放服务端会话，此时为 nil）
    private(set) var activeSessionId: String?
    private(set) var playsOriginalFile = false
    private(set) var originMs = 0
    /// 本单元内 MPV 已失败过：不再尝试，直接走系统播放器
    private var mpvFailed = false
    /// 已因线路不够降到转码档（自动模式下这时交给 AVPlayer 放 HLS）
    private var bandwidthDegraded = false
    private var bandwidthRestarted = false
    /// 用户是否想要播放（程序性暂停——换流、换轨——不改它）
    private(set) var wantsPlay = true
    /// 心跳发现会话没了、但用户正暂停着：等他点播放再重开
    private var deadSession = false

    // MARK: 选择与偏好

    private(set) var subtitles = SubtitleTracks()
    private(set) var selectedSubtitle: String?
    private var subtitleTouched = false
    private var requestedSubtitle: String?
    private var requestedAudio: String?
    private(set) var audioOptions: [AudioOption] = []
    private(set) var currentAudio: String?
    var subtitleStyle = PlayerPreferences.subtitleStyle {
        didSet {
            PlayerPreferences.subtitleStyle = subtitleStyle
            engine?.applySubtitleStyle(subtitleStyle)
        }
    }
    private(set) var quality = PlayerPreferences.quality
    private(set) var enginePreference = PlayerPreferences.engine

    // MARK: 实时读数

    private(set) var positionMs = 0
    private(set) var durationMs: Int?
    private(set) var bufferedEndMs: Int?
    private(set) var paused = true
    private(set) var speedLabel: String?
    private(set) var pipActive = false
    private(set) var notice: String?
    private(set) var holdSpeedActive = false
    private(set) var trickplay: API.TrickplayView?
    private(set) var serverDiagnostics: API.PlaybackDiagnosticsView?
    var diagnosticsOpen = false {
        didSet { restartDiagnosticsPolling() }
    }
    var nextDismissed = false

    // MARK: 内部

    private var startMsOverride: Int?
    private var overrideConsumed = false
    private var reportedStart = false
    private var lastDownlinkBps: Double?
    private var directShortSamples = 0
    private var directHintShown = false
    private var qoe = QoE()
    private var startTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var progressTask: Task<Void, Never>?
    private var tickTask: Task<Void, Never>?
    private var diagnosticsTask: Task<Void, Never>?
    private var trickplayTask: Task<Void, Never>?
    private var noticeTask: Task<Void, Never>?
    private let nowPlaying = NowPlayingBridge()
    private var closed = false

    init(request: PlayRequest, api: APIClient) {
        self.request = request
        scope = PlaybackAPI(api: api, shareSlug: request.shareSlug)
        unit = PlaybackUnit(mediaItemId: request.mediaItemId, season: request.season ?? 0, episode: request.episode ?? 0)
        startMsOverride = request.startSeconds.map { Int($0 * 1000) }
    }

    // MARK: - 生命周期

    /// 播放器出现：加载条目信息（不挡起播）、开始第一个单元
    func start() {
        activateAudioSession()
        nowPlaying.attach(to: self)
        startTickLoop()
        Task { await loadInfo() }
        startUnit(unit)
    }

    /// 退出播放器：补一次停止上报与质量快照、释放服务端会话、销毁引擎
    func close() {
        guard !closed else { return }
        closed = true
        leaveUnit()
        startTask?.cancel()
        tickTask?.cancel()
        diagnosticsTask?.cancel()
        trickplayTask?.cancel()
        noticeTask?.cancel()
        engine?.destroy()
        engine = nil
        nowPlaying.detach()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func loadInfo() async {
        do {
            info = try await scope.item(request.mediaItemId)
            nowPlaying.update(controller: self)
        } catch is CancellationError {
        } catch {
            // 条目信息只影响标题/海报；拿不到时不挡播放，但要让用户看到原因
            infoError = error.localizedDescription
        }
        await loadEpisodes()
    }

    private func loadEpisodes() async {
        guard unit.isEpisode else { episodes = []; return }
        episodes = (try? await scope.episodes(unit.mediaItemId, season: unit.season)) ?? []
        nowPlaying.update(controller: self)
    }

    // MARK: - 单元切换

    /// 开始播放一个单元（首次进入、上一集/下一集）
    func startUnit(_ next: PlaybackUnit) {
        let seasonChanged = next.season != unit.season
        if engine != nil || session != nil { leaveUnit() }
        unit = next
        engine?.destroy()
        engine = nil
        session = nil
        activeSessionId = nil
        pendingDecision = nil
        subtitles = SubtitleTracks()
        selectedSubtitle = nil
        subtitleTouched = false
        requestedAudio = nil
        requestedSubtitle = nil
        audioOptions = []
        currentAudio = nil
        trickplay = nil
        durationMs = nil
        bufferedEndMs = nil
        nextDismissed = false
        mpvFailed = false
        bandwidthDegraded = false
        failedTiers = []
        failureCount = 0
        consentGranted = false
        deadSession = false
        wantsPlay = true
        qoe = QoE()
        // `startSeconds` 只覆盖进入播放器的第一个单元；其余交给服务端按观看状态定起点
        var start: Int? = overrideConsumed ? nil : startMsOverride
        overrideConsumed = true
        if start == nil, let slug = scope.shareSlug, let local = ShareLocalProgress.read(slug, next) {
            // 分享访客的续播点只在本机
            start = local.positionMs
        }
        positionMs = start ?? 0
        if seasonChanged { Task { await loadEpisodes() } }
        nowPlaying.update(controller: self)
        request(startMs: start, phase: .deciding)
    }

    /// 离开当前单元：停止上报（带轨记忆）+ 质量快照 + 释放会话
    private func leaveUnit() {
        pingTask?.cancel()
        progressTask?.cancel()
        if reportedStart {
            reportedStart = false
            let unit = self.unit, position = positionMs, audio = currentAudio, subtitle = subtitleMemory
            Task { await scope.progress(unit, event: "stop", positionMs: position, audio: audio, subtitle: subtitle) }
            reportMetric()
        }
        if let activeSessionId {
            let scope = self.scope
            Task { await scope.stop(activeSessionId) }
        }
        activeSessionId = nil
    }

    // MARK: - 上一集 / 下一集

    /// 下一集只在**本季且有在位文件**里找——缺集要跳过（同 Web player-page）
    var nextEpisode: API.EpisodeView? {
        guard unit.isEpisode else { return nil }
        return episodes.filter { $0.episodeNumber > unit.episode && $0.owned }.min { $0.episodeNumber < $1.episodeNumber }
    }

    var previousEpisode: API.EpisodeView? {
        guard unit.isEpisode else { return nil }
        return episodes.filter { $0.episodeNumber < unit.episode && $0.owned }.max { $0.episodeNumber < $1.episodeNumber }
    }

    var currentEpisode: API.EpisodeView? { episodes.first { $0.episodeNumber == unit.episode } }

    func episodeLabel(_ episode: API.EpisodeView?) -> String? {
        guard unit.isEpisode else { return nil }
        let number = episode?.episodeNumber ?? unit.episode
        let code = String(format: "S%02dE%02d", unit.season, number)
        if let name = episode?.name, !name.isEmpty { return "\(code) · \(name)" }
        return code
    }

    var title: String { info?.title ?? "正在播放" }

    func playNext() {
        guard let next = nextEpisode else { return }
        startUnit(PlaybackUnit(mediaItemId: unit.mediaItemId, season: unit.season, episode: next.episodeNumber))
    }

    func playPrevious() {
        guard let previous = previousEpisode else { return }
        startUnit(PlaybackUnit(mediaItemId: unit.mediaItemId, season: unit.season, episode: previous.episodeNumber))
    }

    /// 片尾 40 秒内（或已播完）显示「即将播放」卡片；不自动倒计时，换集由用户决定
    var showsUpNext: Bool {
        guard nextEpisode != nil, !nextDismissed else { return false }
        if phase == .ended { return true }
        guard let durationMs, durationMs > 0 else { return false }
        let remaining = Double(durationMs - positionMs) / 1000
        return remaining > 0 && remaining <= 40
    }

    // MARK: - 起播（决策 / 降档 / 换会话）

    /// 去后端要一个能播的地址。phase：deciding（新请求）/ sessionStarting（换会话）/ degrading（降档）
    private func request(startMs: Int?, phase next: Phase) {
        attempt += 1
        let myAttempt = attempt
        phase = next
        errorMessage = nil
        errorSuggestion = nil
        pendingDecision = nil
        engine?.pause()
        if qoe.requestedAt == nil { qoe.requestedAt = Date() }
        startTask?.cancel()
        startTask = Task { [weak self] in
            await self?.performRequest(startMs: startMs, attempt: myAttempt)
        }
    }

    private func sessionBody(capability: API.ClientCapabilityIn, startMs: Int?, forMPV: Bool) -> API.PlaybackSessionRequest {
        API.PlaybackSessionRequest(
            fileId: unit.mediaItemId == request.mediaItemId ? request.fileId : nil,
            mediaItemId: unit.mediaItemId,
            seasonNumber: unit.season,
            episodeNumber: unit.episode,
            capability: capability,
            failedTiers: failedTiers.isEmpty ? nil : failedTiers,
            audioTrack: requestedAudio,
            // MPV 自己渲染图形字幕，永远不需要服务端烧录；显式传 off 挡住记忆轨触发的烧录
            subtitleTrack: forMPV ? "off" : requestedSubtitle,
            maxHeight: quality,
            downlinkBps: lastDownlinkBps.map { Int($0) },
            startMs: startMs
        )
    }

    /// MPV 在这台设备上能不能用（本单元失败过就不再试）
    private var mpvAvailable: Bool { !mpvFailed }

    private func performRequest(startMs: Int?, attempt myAttempt: Int) async {
        do {
            // 1. 选引擎
            var useMPV: Bool
            switch enginePreference {
            case .system:
                useMPV = false
            case .mpv:
                useMPV = mpvAvailable
            case .auto:
                if !mpvAvailable {
                    useMPV = false
                } else {
                    let probe = try await scope.decide(sessionBody(capability: PlayerCapability.avPlayer(), startMs: startMs, forMPV: false))
                    guard myAttempt == attempt else { return }
                    if probe.outcome == "plan", probe.tier == 0 {
                        useMPV = false
                    } else if probe.outcome == "plan", probe.video?.action == "transcode", quality != nil || bandwidthDegraded {
                        // 用户自己限了画质 / 线路不够：本来就要服务端转码，交给 AVPlayer 放 HLS（能画中画、投屏）
                        useMPV = false
                    } else {
                        useMPV = true
                    }
                }
            }

            // 2. 开会话
            let capability = useMPV ? PlayerCapability.mpv() : PlayerCapability.avPlayer()
            let session = try await scope.startSession(sessionBody(capability: capability, startMs: startMs, forMPV: useMPV))
            guard myAttempt == attempt, !closed else {
                // 请求已被超越：响应里可能带着刚拉起的转码会话，此后没人认领，当场掐掉
                if let sid = session.sessionId { await scope.stop(sid) }
                return
            }
            await handleSession(session, useMPV: useMPV, requestedStartMs: startMs)
        } catch is CancellationError {
        } catch {
            guard myAttempt == attempt else { return }
            fail(error.localizedDescription, suggestion: nil)
        }
    }

    private func handleSession(_ session: API.PlaybackSessionView, useMPV: Bool, requestedStartMs: Int?) async {
        let decision = session.decision
        switch decision.outcome {
        case "consent":
            if let requestedSubtitle, requestedSubtitle != "off" {
                // 烧录撞上软件转码同意：自动退回旁挂渲染，不打断观看（同 Web）
                self.requestedSubtitle = "off"
                request(startMs: positionMs, phase: .sessionStarting)
                return
            }
            if consentGranted {
                fail("软件转码开关已保存，但服务端仍在请求开启确认",
                     suggestion: "开关可能没有生效（例如服务端刚重启）。请退出重试；若反复出现，请查看服务端日志排查。")
                return
            }
            pendingDecision = decision
            phase = .consent
            return
        case "rejected":
            fail(decision.reason, suggestion: decision.suggestion)
            return
        default:
            consentGranted = false
        }
        guard let fileId = decision.fileId else {
            fail("服务端没有给出可播放的文件", suggestion: nil)
            return
        }

        // 旧会话（换轨/换画质/降档之前那条）：新会话已就位，释放它
        if let old = activeSessionId, old != session.sessionId {
            let scope = self.scope
            Task { await scope.stop(old) }
        }

        // 3. 决定地址与时间轴
        let videoCopy = decision.video?.action == "copy" && decision.video?.burnSubtitle == nil
        var url: URL?
        var original = false
        if useMPV, videoCopy, let token = PlaybackAPI.token(in: session.streamUrl) {
            // MPV 直出原文件：服务端为 remux/音频转码起的会话用不上，立刻释放
            url = scope.streamURL("/api/v1/playback/files/\(fileId)/stream?token=\(token)")
            original = true
            if let sid = session.sessionId { let scope = self.scope; Task { await scope.stop(sid) } }
            activeSessionId = nil
        } else {
            url = scope.streamURL(session.streamUrl)
            activeSessionId = session.sessionId
        }
        guard let url else {
            fail("服务端没有给出播放地址", suggestion: nil)
            return
        }
        playsOriginalFile = original || decision.tier == 0
        originMs = (playsOriginalFile || session.timeline == "file") ? 0 : session.startMs
        if requestedStartMs == nil || positionMs == 0 { positionMs = session.startMs }
        if let duration = session.watch?.durationMs { durationMs = duration }
        self.session = session

        // 4. 轨道
        subtitles = SubtitleTracks.plan(decision.subtitles, urls: session.subtitleUrls)
        audioOptions = AudioOption.plan(decision.audioTracks)
        currentAudio = requestedAudio ?? decision.audio?.trackRef
        if let burned = decision.video?.burnSubtitle {
            selectedSubtitle = burned
        } else if !subtitleTouched {
            let remembered = scope.shareSlug.flatMap { ShareLocalProgress.read($0, unit)?.subtitleTrack } ?? session.watch?.subtitleTrack
            selectedSubtitle = subtitles.initialSelection(remembered: remembered)
        }

        // 5. 挂引擎
        let newEngine: any PlayerEngine
        if useMPV {
            do {
                newEngine = try MPVEngine(playsOriginalFile: original)
            } catch {
                mpvFallback(reason: error.localizedDescription)
                return
            }
        } else {
            newEngine = AVPlayerEngine()
        }
        engine?.destroy()
        engine = newEngine
        newEngine.onEvent = { [weak self, weak newEngine] event in
            guard let self, let newEngine, self.engine === newEngine else { return }
            self.handleEngineEvent(event)
        }
        newEngine.applySubtitleStyle(subtitleStyle)
        let startSeconds = Double(max(0, positionMs - originMs)) / 1000
        newEngine.load(url: url, start: startSeconds, autoplay: wantsPlay)
        if original, let index = decision.audio?.trackRef.flatMap({ AudioOption(ref: $0, label: "", isDefault: false).embeddedIndex }) {
            newEngine.selectAudio(embeddedIndex: index)
        }
        applySubtitleToEngine()
        phase = .buffering
        deadSession = false
        bandwidthRestarted = false
        directShortSamples = 0
        directHintShown = false
        startPingLoop()
        loadTrickplay(fileId: fileId, token: PlaybackAPI.token(in: session.streamUrl))
        restartDiagnosticsPolling()
        nowPlaying.update(controller: self)
    }

    private func fail(_ message: String, suggestion: String?) {
        engine?.pause()
        phase = .error
        errorMessage = message
        errorSuggestion = suggestion
        session = nil
    }

    /// MPV 不可用 / 放不了：本单元改走服务端 HLS + 系统播放器
    private func mpvFallback(reason: String) {
        mpvFailed = true
        scope.clientLog("engine-fallback", [
            "from": .string("mpv"), "reason": .string(reason),
            "media_item_id": .int(unit.mediaItemId),
        ])
        flash("MPV 播放失败，已改用系统播放器")
        request(startMs: positionMs, phase: .sessionStarting)
    }

    // MARK: - 用户操作：重试 / 同意

    func retry() {
        failedTiers = []
        failureCount = 0
        request(startMs: positionMs, phase: .deciding)
    }

    /// 同意弹窗「开启并播放」：写入全局开关后重新决策
    func grantConsent() async throws {
        let saved = try await scope.api.playbackPolicySet(body: API.PlaybackPolicyPayload(softwareTranscodeEnabled: true))
        // 保存接口回显的是落库后的取值：不是 true 说明开关根本没生效，不能假装成功
        guard saved.softwareTranscodeEnabled else {
            throw APIError.http(status: 500, message: "软件转码开关保存后未生效，请重试或查看服务端日志", code: nil)
        }
        consentGranted = true
        request(startMs: positionMs, phase: .deciding)
    }

    // MARK: - 引擎事件

    private func handleEngineEvent(_ event: EngineEvent) {
        switch event {
        case .playing:
            paused = false
            guard [.buffering, .playing, .ended].contains(phase) else { return }
            if phase == .buffering, qoe.bufferingSince != nil { qoe.endRebuffer() }
            phase = .playing
            failureCount = 0
            if qoe.ttffMs == nil, let requestedAt = qoe.requestedAt {
                qoe.ttffMs = Int(Date().timeIntervalSince(requestedAt) * 1000)
            }
            if !reportedStart {
                reportedStart = true
                let unit = self.unit, audio = currentAudio, subtitle = subtitleMemory
                Task {
                    let state = await scope.progress(unit, event: "start", positionMs: nil, audio: audio, subtitle: subtitle)
                    handleProgressResponse(state)
                }
                startProgressLoop()
            } else {
                sendProgress(paused: false)
            }
        case .paused:
            paused = true
            if reportedStart { sendProgress(paused: true) }
        case .buffering:
            if phase == .playing {
                phase = .buffering
                qoe.beginRebuffer()
            }
            if holdSpeedActive, (engine?.bufferedEnd ?? 0) - (engine?.currentTime ?? 0) < 1 {
                // 倍速吃光了前向缓冲：退回原速
                endHoldSpeed()
                flash("缓冲跟不上，已退出 2 倍速")
            }
        case .ended:
            guard [.buffering, .playing].contains(phase) else { return }
            phase = .ended
            paused = true
            if let durationMs { positionMs = durationMs }
            if reportedStart { sendProgress(paused: true) }
        case let .failed(reason, cause):
            engineFailed(reason: reason, cause: cause)
        case let .pictureInPicture(active):
            pipActive = active
        }
    }

    /// 播放链路失败：MPV → 回落系统播放器；AVPlayer → 按带宽重开 / 越级转码 / 逐级降档（同 Web）
    private func engineFailed(reason: String, cause: EngineFailureCause) {
        guard [.buffering, .playing, .ended].contains(phase), let session else { return }
        scope.clientLog("playback-error", [
            "engine": .string(engine?.kind.rawValue ?? ""), "reason": .string(reason),
            "tier": session.decision.tier.map { .int($0) } ?? .null,
        ])
        if engine?.kind == .mpv {
            mpvFallback(reason: reason)
            return
        }
        let stats = engine?.stats()
        let downlink = stats?.downlinkBps
        let bitrate = stats?.bitrateBps ?? session.source?.bitRate.map(Double.init)
        let videoAction = session.decision.video?.action
        if cause == .starved, let downlink, let bitrate, downlink > 0, bitrate > 0, downlink < bitrate {
            lastDownlinkBps = downlink
            if videoAction == "transcode", !bandwidthRestarted {
                // 已在转码且线路不够：带着实测带宽同档重开（服务端按它压码率），每会话一次
                bandwidthRestarted = true
                scope.clientLog("bandwidth-restart", ["downlink_bps": .int(Int(downlink)), "bitrate_bps": .int(Int(bitrate))])
                request(startMs: positionMs, phase: .sessionStarting)
                return
            }
            if videoAction == "copy" {
                // 视频直通而线路装不下源码率：三个直通档一并标掉，直接开转码会话
                bandwidthDegraded = true
                failedTiers = Array(Set(failedTiers + [0, 1, 2])).sorted()
                scope.clientLog("bandwidth-degrade", [
                    "tier": session.decision.tier.map { .int($0) } ?? .null,
                    "downlink_bps": .int(Int(downlink)), "bitrate_bps": .int(Int(bitrate)),
                ])
                flash("线路带宽装不下原片码率，改用转码降码率播放")
                request(startMs: positionMs, phase: .degrading)
                return
            }
        }
        guard let tier = session.decision.tier else {
            fail(reason, suggestion: nil)
            return
        }
        // 逐级降档；连败两次说明「逐级试」的假设不成立，兜底档以下全标失败一步到位
        var accumulated = Set(failedTiers)
        accumulated.insert(tier)
        if failureCount + 1 >= 2 { (0 ..< 4).forEach { accumulated.insert($0) } }
        failedTiers = accumulated.sorted()
        failureCount += 1
        if tier >= 4 {
            fail(reason, suggestion: "这个文件在系统播放器里放不出来，可以在「⋯ → 播放引擎」换用 MPV 再试。")
            return
        }
        request(startMs: positionMs, phase: .degrading)
    }

    // MARK: - 播放控制

    func togglePlay() {
        guard let engine else { return }
        if phase == .ended {
            seek(toFileMs: 0)
            wantsPlay = true
            engine.play()
            return
        }
        if engine.isPaused {
            wantsPlay = true
            if deadSession {
                // 会话在暂停期间被回收了：从当前位置重开
                request(startMs: positionMs, phase: .sessionStarting)
                return
            }
            engine.play()
        } else {
            wantsPlay = false
            engine.pause()
        }
    }

    func play() { if engine?.isPaused == true { togglePlay() } }
    func pause() { if engine?.isPaused == false { togglePlay() } }

    /// 相对跳转（±10 秒按钮、双击、锁屏遥控）：按关键帧，快
    func seek(by seconds: Double) {
        seek(toFileMs: positionMs + Int(seconds * 1000), exact: false)
    }

    /// 跳到文件时间（毫秒）
    func seek(toFileMs raw: Int, exact: Bool = true) {
        // 先夹进片长之内：越过片尾的落点会开出一个什么也转不出来的会话
        var target = max(0, raw)
        if let durationMs, durationMs > 1000 { target = min(target, durationMs - 1000) }
        qoe.seekCount += 1
        guard let engine, session != nil, phase != .sessionStarting, phase != .deciding, phase != .degrading else {
            // 会话正在重开的空档：改走换会话，新会话直接从目标位置起
            if phase.isBusy, session == nil, phase != .deciding || positionMs > 0 {
                positionMs = target
                request(startMs: target, phase: .sessionStarting)
            }
            return
        }
        if session?.timeline == "session", activeSessionId != nil {
            // 旧式会话相对列表只能从起点往后转：拖出区间必须换会话
            positionMs = target
            request(startMs: target, phase: .sessionStarting)
            return
        }
        positionMs = target
        if phase == .ended { phase = .buffering }
        engine.seek(to: Double(target - originMs) / 1000, exact: exact)
    }

    // MARK: 长按 2 倍速

    func beginHoldSpeed() -> Bool {
        guard let engine, !engine.isPaused, phase == .playing else { return false }
        holdSpeedActive = true
        engine.setRate(2)
        return true
    }

    func endHoldSpeed() {
        guard holdSpeedActive else { return }
        holdSpeedActive = false
        engine?.setRate(1)
    }

    // MARK: 画中画

    func togglePictureInPicture() { engine?.togglePictureInPicture() }

    // MARK: - 音轨 / 字幕 / 画质 / 引擎

    /// 换音轨：MPV 直出原文件时原地切换；否则带着当前位置重开会话（音轨在开会话时就定死了）
    func selectAudio(_ ref: String) {
        requestedAudio = ref
        if let engine, engine.canSwitchAudioInPlace, let index = AudioOption(ref: ref, label: "", isDefault: false).embeddedIndex {
            engine.selectAudio(embeddedIndex: index)
            currentAudio = ref
            sendProgress(paused: engine.isPaused)
            return
        }
        guard ref != (session?.decision.audio?.trackRef) else { return }
        wantsPlay = true
        request(startMs: positionMs, phase: .sessionStarting)
    }

    /// 换音轨是否需要重开会话（菜单里据此显示提示）
    var audioSwitchRestarts: Bool { !(engine?.canSwitchAudioInPlace ?? false) }

    /// 当前正在服务端烧录的字幕轨
    var burnedSubtitle: String? { session?.decision.video?.burnSubtitle }

    /// 字幕由 MPV 自己渲染（样式/时间轴都作用在 mpv 上）
    var engineRendersSubtitles: Bool { engine?.rendersSubtitles ?? false }

    func selectSubtitle(_ ref: String?) {
        subtitleTouched = true
        selectedSubtitle = ref
        let target = ref.flatMap { ref in subtitles.options.first { $0.ref == ref } }
        if engineRendersSubtitles {
            applySubtitleToEngine()
            if burnedSubtitle != nil {
                // MPV 在放烧录过的转码流：撤下烧录
                requestedSubtitle = "off"
                request(startMs: positionMs, phase: .sessionStarting)
            }
            if reportedStart { sendProgress(paused: paused) }
            return
        }
        let wantBurn = target?.kind == "pgs"
        if !wantBurn, burnedSubtitle == nil {
            // 纯文本切换，叠加层搞定
            if reportedStart { sendProgress(paused: paused) }
            return
        }
        if wantBurn, burnedSubtitle == ref { return }
        // 图形字幕（PGS）：系统播放器渲染不了，服务端转码压制进画面（约一秒切换）
        requestedSubtitle = ref ?? "off"
        wantsPlay = true
        request(startMs: positionMs, phase: .sessionStarting)
    }

    /// 叠加层要渲染的文本字幕（AVPlayer 模式）：ASS 由服务端转成 VTT 纯文本
    var overlaySubtitleURL: URL? {
        guard !engineRendersSubtitles, burnedSubtitle == nil, let ref = selectedSubtitle,
              let option = subtitles.options.first(where: { $0.ref == ref }), option.kind != "pgs" else { return nil }
        return scope.streamURL(option.path + "&format=vtt")
    }

    private func applySubtitleToEngine() {
        guard let engine, engine.rendersSubtitles else { return }
        let option = selectedSubtitle.flatMap { ref in subtitles.options.first { $0.ref == ref } }
        engine.selectSubtitle(option, url: option.flatMap { scope.streamURL($0.path) })
    }

    /// 上报用的字幕记忆："off" = 用户明确关掉
    private var subtitleMemory: String? {
        guard session != nil || subtitleTouched else { return nil }
        return selectedSubtitle ?? "off"
    }

    func selectQuality(_ maxHeight: Int?) {
        PlayerPreferences.quality = maxHeight
        guard maxHeight != quality else { return }
        quality = maxHeight
        // 语义是上限：视频直通且源不超所选档就不用动
        let copying = session?.decision.video?.action == "copy"
        let height = Int(engine?.videoSize.height ?? 0)
        if copying, maxHeight == nil || (height > 0 && height <= maxHeight!) { return }
        wantsPlay = true
        failedTiers = []
        failureCount = 0
        request(startMs: positionMs, phase: .deciding)
    }

    func selectEngine(_ preference: EnginePreference) {
        PlayerPreferences.engine = preference
        guard preference != enginePreference else { return }
        enginePreference = preference
        mpvFailed = false
        failedTiers = []
        failureCount = 0
        wantsPlay = !(engine?.isPaused ?? false) || wantsPlay
        request(startMs: positionMs, phase: .deciding)
    }

    // MARK: - 前后台

    func setBackgrounded(_ background: Bool) {
        engine?.setBackgrounded(background)
        if !background, let activeSessionId {
            // 回前台先探一次活：后台期间心跳可能被系统挂起、会话已被回收
            Task { await ping(activeSessionId) }
        }
    }

    // MARK: - 心跳 / 进度

    private func startPingLoop() {
        pingTask?.cancel()
        guard let sessionId = activeSessionId else { return }
        pingTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                if Task.isCancelled { return }
                await self?.ping(sessionId)
            }
        }
    }

    private func ping(_ sessionId: String) async {
        let alive = await scope.ping(sessionId)
        // nil = 这次请求本身失败（断网/5xx），不能据此判定会话没了
        guard alive == false, sessionId == activeSessionId else { return }
        guard [.playing, .buffering].contains(phase) else { return }
        if engine?.isPaused == true, !wantsPlay {
            // 用户自己暂停着：不替他白烧一路转码，等他点播放再重开
            deadSession = true
            return
        }
        request(startMs: positionMs, phase: .sessionStarting)
    }

    private func startProgressLoop() {
        progressTask?.cancel()
        progressTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(10))
                if Task.isCancelled { return }
                guard let self, self.phase == .playing else { continue }
                self.sendProgress(paused: self.engine?.isPaused)
            }
        }
    }

    private func sendProgress(paused: Bool?) {
        guard reportedStart else { return }
        let unit = self.unit, position = positionMs, audio = currentAudio, subtitle = subtitleMemory
        Task {
            let state = await scope.progress(unit, event: "progress", positionMs: position, paused: paused, audio: audio, subtitle: subtitle)
            handleProgressResponse(state)
        }
    }

    /// 管理员在活动页结束了本次播放：退出并说明（服务端同时进入拒绝窗口，不能走「会话没了就重开」）
    private func handleProgressResponse(_ state: API.PlaybackStateView?) {
        guard state?.endedByAdmin == true, phase != .error else { return }
        engine?.pause()
        leaveUnit()
        fail("管理员已结束本次播放", suggestion: "稍后可以重新开始播放；观看进度已经保存。")
    }

    private func reportMetric() {
        guard let decision = session?.decision, let tier = decision.tier else { return }
        let stats = engine?.stats()
        qoe.flushWatched()
        guard qoe.watchedMs > 0 || qoe.ttffMs != nil else { return }
        scope.metric(API.PlaybackMetricPayload(
            libraryFileId: decision.fileId, tier: tier, degradedFrom: decision.degradedFrom,
            engine: engine?.kind.rawValue ?? "", hwBackend: session?.hwBackend ?? "",
            ttffMs: qoe.ttffMs, rebufferMs: qoe.rebufferMs, rebufferCount: qoe.rebufferCount,
            seekCount: qoe.seekCount, droppedFrames: stats?.droppedFrames, totalFrames: stats?.totalFrames,
            watchedMs: qoe.watchedMs
        ))
    }

    // MARK: - 读数循环（4Hz 位置 / 1Hz 速度）

    private func startTickLoop() {
        tickTask?.cancel()
        tickTask = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(250))
                guard let self else { return }
                self.tickPosition()
                tick += 1
                if tick % 4 == 0 { self.tickSecond() }
            }
        }
    }

    private func tickPosition() {
        guard let engine, session != nil, [.buffering, .playing].contains(phase) else { return }
        let stream = engine.currentTime
        let file = originMs + Int(stream * 1000)
        // 起播 seek 落地前引擎报的是 0：别让进度条先闪回片头
        if !(stream < 0.05 && positionMs > 2000 && phase == .buffering) { positionMs = file }
        if durationMs == nil, let duration = engine.duration { durationMs = originMs + Int(duration * 1000) }
        bufferedEndMs = engine.bufferedEnd.map { originMs + Int($0 * 1000) }
        paused = engine.isPaused
        if phase == .playing, !engine.isPaused { qoe.tickWatched() } else { qoe.pauseWatched() }
    }

    private func tickSecond() {
        guard let engine else { return }
        let stats = engine.stats()
        if let label = Self.formatBandwidth(stats.downlinkBps) { speedLabel = label }
        if let downlink = stats.downlinkBps { lastDownlinkBps = downlink }
        // 直通线路不够：码率改不了，只能提醒换画质。连续 10 次采样不够才提示、每会话一次
        if playsOriginalFile, !directHintShown, let downlink = stats.downlinkBps, downlink > 0,
           let source = session?.source?.bitRate, source > 0 {
            directShortSamples = downlink < Double(source) * 1.2 ? directShortSamples + 1 : 0
            if directShortSamples >= 10 {
                directHintShown = true
                flash("线路速度低于片源码率，可在设置里选更低画质")
            }
        }
        nowPlaying.updatePosition(controller: self)
    }

    /// bps → 「3.2 MB/s」（用户对下载速度的直觉来自下载器，一律 MB/s，进位 1024；同 Web formatBandwidth）
    static func formatBandwidth(_ bps: Double?) -> String? {
        guard let bps, bps.isFinite, bps > 0 else { return nil }
        let bytes = bps / 8
        let mb = bytes / (1024 * 1024)
        if mb >= 1 { return String(format: "%.1f MB/s", mb) }
        let kb = bytes / 1024
        return kb < 1 ? "0 KB/s" : "\(Int(kb.rounded())) KB/s"
    }

    // MARK: - 诊断 / 缩略图

    private func restartDiagnosticsPolling() {
        diagnosticsTask?.cancel()
        serverDiagnostics = nil
        guard diagnosticsOpen, let sessionId = activeSessionId, let token = PlaybackAPI.token(in: session?.streamUrl) else { return }
        diagnosticsTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                // 诊断是旁路信息：服务端瞬时不可用时保留上一份快照
                if let snapshot = try? await self.scope.diagnostics(sessionId, token: token) {
                    self.serverDiagnostics = snapshot
                }
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    /// 进度条缩略图索引：开会话时服务端才在后台生成，没就绪就隔一阵再问（最多几次，失败无所谓）
    private func loadTrickplay(fileId: Int, token: String?) {
        trickplayTask?.cancel()
        guard let token else { return }
        trickplayTask = Task { [weak self] in
            for _ in 0 ..< 6 {
                guard let self else { return }
                if let index = try? await self.scope.api.playbackFileTrickplay(fileId: fileId, token: token), index.ready {
                    self.trickplay = index
                    return
                }
                try? await Task.sleep(for: .seconds(30))
            }
        }
    }

    // MARK: - 提示

    func flash(_ message: String) {
        notice = message
        noticeTask?.cancel()
        noticeTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            if !Task.isCancelled { self?.notice = nil }
        }
    }

    private func activateAudioSession() {
        let audio = AVAudioSession.sharedInstance()
        try? audio.setCategory(.playback, mode: .moviePlayback, policy: .longFormVideo)
        try? audio.setActive(true)
    }
}

/// 一次播放的质量读数（对应 Web `lib/player/qoe.ts` 的归约结果）
private struct QoE {
    var requestedAt: Date?
    var ttffMs: Int?
    var rebufferCount = 0
    var rebufferMs = 0
    var seekCount = 0
    var watchedMs = 0
    var bufferingSince: Date?
    private var watchingSince: Date?

    mutating func beginRebuffer() {
        rebufferCount += 1
        bufferingSince = Date()
    }

    mutating func endRebuffer() {
        if let since = bufferingSince { rebufferMs += Int(Date().timeIntervalSince(since) * 1000) }
        bufferingSince = nil
    }

    mutating func tickWatched() {
        if watchingSince == nil { watchingSince = Date() }
    }

    mutating func pauseWatched() {
        flushWatched()
    }

    mutating func flushWatched() {
        if let since = watchingSince { watchedMs += Int(Date().timeIntervalSince(since) * 1000) }
        watchingSince = nil
    }
}
