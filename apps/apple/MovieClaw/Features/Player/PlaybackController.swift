import AVFoundation
import AVKit
import SwiftUI

/// 播放控制器：会话协议 + 状态机 + 引擎编排（对应 Web `components/player/video-player.tsx` 与 `lib/player/machine.ts`）。
///
/// ## 起播链路
/// 「决策 → 开会话 → 挂引擎 → 缓冲 → 出画」四段异步，中间随时可能插进来 seek、换轨、降档、切集。
/// 为此每次「去后端要一个能播的地址」都带一个递增的 `attempt` 序号：响应回来时序号已被超越
/// （用户又换了参数/退出了），就把刚拉起的会话当场掐掉，绝不让它变成占着转码名额的孤儿。
///
/// ## 引擎选择（全自动，用户不选；docs/design/ios-app.md §4）
/// 系统播放器优先：画中画、隔空播放、杜比视界、系统字体字幕都只有它有，用户在意的是这些，而不是引擎。
/// 先用 AVPlayer 的能力问一次决策（`/decide`，不起会话），服务端能直出或只换封装/转音频（画面不重编码）
/// 就交给 AVPlayer——MKV 换壳成 HLS、DTS/TrueHD 转 AAC 都是轻活。只有两种情况改用 MPV 在本机直接放原文件：
/// 1. 选中的是图形字幕（PGS）：系统播放器画不了，服务端只能把字幕压进画面、整片重新编码（NAS 没有显卡时几乎放不动）；
/// 2. 按 AVPlayer 的能力服务端要重新编码画面（编码不支持、杜比视界要色调映射……）或拒绝/要同意，
///    而且不是因为用户限了画质、线路不够（那本来就要转码，交给 AVPlayer 放 HLS）。
/// 系统播放器在不重编码的档位放不出来时自动改用 MPV；MPV 出任何问题都回落服务端 HLS + AVPlayer，本单元不再试 MPV。
/// MPV 没有画中画：播放中点画中画，就在当前位置换成系统播放器、就绪后自动进画中画。
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
    /// 本单元内 MPV 直出原文件失败过（例如多剪辑原盘没有单一原文件）：下次让 MPV 放服务端 HLS
    private var mpvDirectFailed = false
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
    /// 开发期强制引擎（启动参数 `-movieclaw.player.engine system|mpv`），正式版恒为 nil
    private let engineOverride = EngineOverride.current

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
    /// 卡顿归因 / 掉帧看门狗（每秒一个样本，见 PlaybackWatchdogs.swift）
    private var stallWatch = StallWatch()
    private var frameDrops = FrameDropTracker()
    /// 同档网络重开的次数上限（见 NetworkRestartBudget）
    private var networkRestarts = NetworkRestartBudget()
    /// 已发出、还没落地的 seek：这段等待不算卡顿（QoE 口径同 Web qoe.ts），看门狗也不把它当停顿
    private var seekStartedAt: Date?
    private var backgrounded = false
    /// 拖动跟随：上一次真的跟过去的时刻与排队中的后沿落地
    private var lastScrubFollowAt = Date.distantPast
    private var scrubFollowTask: Task<Void, Never>?
    /// 本单元改用 MPV：选了图形字幕（PGS，MPV 在本机画）或系统播放器在不重编码的档位放不出来
    private var preferMPV = false
    /// 本单元为了画中画改用系统播放器（MPV 没有画中画），不再自动换回 MPV
    private var systemForPiP = false
    /// 换成系统播放器后，画面一就绪就自动进画中画
    private var pendingPiP = false
    private var startTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var progressTask: Task<Void, Never>?
    private var tickTask: Task<Void, Never>?
    private var diagnosticsTask: Task<Void, Never>?
    private var trickplayTask: Task<Void, Never>?
    private var noticeTask: Task<Void, Never>?
    private let nowPlaying = NowPlayingBridge()
    private var closed = false
    /// 上报串行队列：start / progress / stop 按发出顺序到达。各自独立的 Task 可能乱序——
    /// stop 先到、进度后到，服务端会把刚结束的会话「复活」，还多开一行永远不收口的播放日志
    private var reportQueue: Task<Void, Never>?
    private var terminationObserver: NSObjectProtocol?

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
        // App 被结束（在后台播放时被划掉、被系统回收）：同步补发一次 stop（同网页 pagehide 的 sendBeacon），
        // 否则续播点停在最后一次心跳、活动页还挂着一个几分钟后才过期的「幽灵」会话
        terminationObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.willTerminateNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.reportTermination() }
        }
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
        if let terminationObserver { NotificationCenter.default.removeObserver(terminationObserver) }
        terminationObserver = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func loadInfo() async {
        do {
            info = try await scope.item(request.mediaItemId)
            nowPlaying.update(controller: self)
        } catch is CancellationError {
        } catch {
            // 条目信息拿不到几乎必然意味着会话也开不了（同一套可见性判据）：同 Web player-page，
            // 整页换成原因 + 「返回」，停掉正在起的播放
            infoError = error.localizedDescription
            attempt += 1
            startTask?.cancel()
            engine?.pause()
            leaveUnit()
            return
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
        mpvDirectFailed = false
        bandwidthDegraded = false
        failedTiers = []
        failureCount = 0
        networkRestarts.reset()
        consentGranted = false
        deadSession = false
        wantsPlay = true
        qoe = QoE()
        preferMPV = false
        systemForPiP = false
        pendingPiP = false
        resetWatchdogs()
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
            let unit = self.unit, position = positionMs, audio = audioMemory, subtitle = subtitleMemory, duration = durationMs
            let scope = self.scope
            enqueueReport {
                await scope.progress(unit, event: "stop", positionMs: position, durationMs: duration, audio: audio, subtitle: subtitle)
                NotificationCenter.default.post(name: .playbackStopReported, object: nil, userInfo: ["mediaItemId": unit.mediaItemId])
            }
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

    /// 进入播放器时的单元（`request.fileId` 只属于它）
    private var initialUnit: PlaybackUnit {
        PlaybackUnit(mediaItemId: request.mediaItemId, season: request.season ?? 0, episode: request.episode ?? 0)
    }

    private func sessionBody(capability: API.ClientCapabilityIn, startMs: Int?, forMPV: Bool) -> API.PlaybackSessionRequest {
        API.PlaybackSessionRequest(
            // 指定版本只对进入播放器的那个单元有效：切到别的集还带着它，会去请求上一集的文件
            fileId: unit == initialUnit ? request.fileId : nil,
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

    /// MPV 在这台设备上能不能用（本单元失败过、或为了画中画换成了系统播放器，就不再试）
    private var mpvAvailable: Bool { !mpvFailed && !systemForPiP && engineOverride != .system }

    private func performRequest(startMs: Int?, attempt myAttempt: Int) async {
        do {
            // 1. 选引擎（规则见类注释）
            var useMPV: Bool
            if engineOverride == .mpv {
                useMPV = !mpvFailed
            } else if !mpvAvailable {
                useMPV = false
            } else if preferMPV {
                useMPV = true
            } else {
                let probe = try await scope.decide(sessionBody(capability: PlayerCapability.avPlayer(), startMs: startMs, forMPV: false))
                guard myAttempt == attempt else { return }
                if probe.outcome == "plan", probe.video?.action != "transcode" {
                    // 直出或只换封装/转音频：系统播放器（画中画、隔空播放、系统字体字幕）
                    useMPV = false
                } else if probe.outcome == "plan", quality != nil || bandwidthDegraded {
                    // 用户自己限了画质 / 线路不够：本来就要服务端转码，交给 AVPlayer 放 HLS
                    useMPV = false
                } else {
                    // 要为系统播放器重新编码画面（含图形字幕压制）、或被拒绝/要同意：MPV 在本机直接放原文件
                    useMPV = true
                }
            }

            // 2. 开会话
            let capability = useMPV ? PlayerCapability.mpv() : PlayerCapability.avPlayer()
            var session = try await scope.startSession(sessionBody(capability: capability, startMs: startMs, forMPV: useMPV))
            if useMPV, session.decision.outcome != "plan", quality == nil, !bandwidthDegraded, myAttempt == attempt {
                // 服务端按「浏览器口径」拒绝了（例如 4K 杜比视界没有显卡做色调映射）或要求同意软转，
                // 但 MPV 自己就能解原片。服务端只在给出计划时签发取流 token，于是按移动端口径再要一次计划，
                // 拿到 token 就直出原文件，顺手释放那条用不上的转码会话。
                let retry = try await scope.startSession(sessionBody(capability: PlayerCapability.mpv(mobileLimited: true), startMs: startMs, forMPV: true))
                if retry.decision.outcome == "plan" { session = retry }
            }
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
                // 烧录撞上软件转码同意：自动退回旁挂渲染，不打断观看（同 Web）。
                // 图形字幕系统播放器画不了，菜单里不能还挂着选中态、画面上却什么都没有
                let dropped = subtitles.options.first { $0.ref == requestedSubtitle }
                self.requestedSubtitle = "off"
                if dropped?.kind == "pgs" {
                    selectedSubtitle = nil
                    subtitleTouched = true
                    flash("图形字幕需要服务端转码压制，当前未开启软件转码，已关闭字幕")
                }
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
        var url: URL?
        var original = false
        if useMPV, mpvShouldPlayOriginal(session), let token = PlaybackAPI.token(in: session.streamUrl) {
            // MPV 直出原文件：服务端为 remux/音频转码起的会话用不上，立刻释放
            url = scope.streamURL("/api/v1/playback/files/\(fileId)/stream?token=\(token)")
            original = true
            if let sid = session.sessionId { let scope = self.scope; Task { await scope.stop(sid) } }
            activeSessionId = nil
        } else if !useMPV, session.timeline == "file", let master = session.masterUrl {
            // AVPlayer 放 VOD：吃 master 列表，里面的 WEBVTT 字幕组让画中画 / 隔空播放时由系统渲染字幕
            url = scope.streamURL(master)
            activeSessionId = session.sessionId
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
            var remembered = scope.shareSlug.flatMap { ShareLocalProgress.read($0, unit)?.subtitleTrack } ?? session.watch?.subtitleTrack
            #if DEBUG
            // 开发期：-mcSubtitle <轨引用>（embedded:N / external:文件名 / off）指定起播字幕，对照两个引擎的字幕渲染用
            if let forced = UserDefaults.standard.string(forKey: "mcSubtitle") { remembered = forced }
            #endif
            selectedSubtitle = subtitles.initialSelection(remembered: remembered)
        }

        if !useMPV, shouldSwitchToMPV(forSubtitle: selectedSubtitle) {
            // 续播记忆 / 默认轨是图形字幕：直接换 MPV 在本机画，不让服务端整片重新编码去压字幕
            preferMPV = true
            if let sid = session.sessionId { let scope = self.scope; Task { await scope.stop(sid) } }
            activeSessionId = nil
            request(startMs: positionMs, phase: .sessionStarting)
            return
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
        resetWatchdogs()
        let startSeconds = Double(max(0, positionMs - originMs)) / 1000
        newEngine.load(url: url, start: startSeconds, autoplay: wantsPlay)
        if original, let index = decision.audio?.trackRef.flatMap({ AudioOption(ref: $0, label: "", isDefault: false).embeddedIndex }) {
            newEngine.selectAudio(embeddedIndex: index)
        }
        if pendingPiP, let avPlayer = newEngine as? AVPlayerEngine {
            // 为画中画换成了系统播放器：画面一就绪就进画中画
            pendingPiP = false
            avPlayer.startPictureInPictureWhenPossible()
        }
        applySubtitleToEngine()
        applySystemSubtitle()
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

    /// MPV 该不该直接拉原文件。MPV 什么编码都能解，服务端判的「要转码」多半只是替浏览器/AVPlayer 着想
    /// （杜比视界色调映射、编码不支持……），这些情况 MPV 直出更好、NAS 也省一路转码。
    /// 只有「码率必须降下来」时才真的需要服务端转码：用户手动限了画质且源超过上限，或线路装不下原片。
    private func mpvShouldPlayOriginal(_ session: API.PlaybackSessionView) -> Bool {
        guard !mpvDirectFailed, !bandwidthDegraded else { return false }
        if let quality, let height = Self.height(of: session.source?.resolution), height > quality { return false }
        return true
    }

    /// 「2160p」「1080i」→ 2160 / 1080
    private static func height(of resolution: String?) -> Int? {
        resolution.flatMap { Int($0.filter(\.isNumber)) }
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
        networkRestarts.reset()
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
            if let since = seekStartedAt {
                qoe.lastSeekMs = Int(Date().timeIntervalSince(since) * 1000)
                seekStartedAt = nil
            }
            guard [.buffering, .playing, .ended].contains(phase) else { return }
            if phase == .buffering, qoe.bufferingSince != nil { qoe.endRebuffer() }
            phase = .playing
            failureCount = 0
            networkRestarts.reachedPlaying()
            if qoe.ttffMs == nil, let requestedAt = qoe.requestedAt {
                qoe.ttffMs = Int(Date().timeIntervalSince(requestedAt) * 1000)
            }
            if !reportedStart {
                reportedStart = true
                let unit = self.unit, audio = audioMemory, subtitle = subtitleMemory
                let scope = self.scope
                enqueueReport { [weak self] in
                    let state = await scope.progress(unit, event: "start", positionMs: nil, audio: audio, subtitle: subtitle)
                    self?.handleProgressResponse(state)
                }
                startProgressLoop()
            } else {
                sendProgress(paused: false)
            }
        case .paused:
            paused = true
            seekStartedAt = nil
            // 引擎已就绪却停在「缓冲」（暂停中拖动、以暂停状态起播）：回到正常态，否则转圈不消、10 秒心跳也停发，
            // 活动页几分钟后就把这个会话丢了
            if phase == .buffering, engine?.duration != nil {
                if qoe.bufferingSince != nil { qoe.endRebuffer() }
                phase = .playing
            }
            // 换会话 / 降档时控制器自己按的暂停（request 里的 engine.pause）不上报：那不是用户暂停
            if reportedStart, !phase.isBusy { sendProgress(paused: true) }
        case .buffering:
            if phase == .playing {
                phase = .buffering
                // seek 造成的等待是「跳转耗时」，不是卡顿（同 Web qoe.ts 口径）
                if seekStartedAt == nil { qoe.beginRebuffer() }
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
            // 引擎报「播完」不一定真到了片尾（MPV 断流也会报 eof）：离片尾 5 秒内才吸附到片长，
            // 否则按真实位置上报——片长会让服务端直接标「已看」
            if let durationMs, durationMs - positionMs <= 5000 { positionMs = durationMs }
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
            if playsOriginalFile, session.sessionId != nil || session.decision.tier != 0, !mpvDirectFailed {
                // 原文件拉不下来（原盘多剪辑、网盘直链失效……）：先让 MPV 改放服务端 HLS 再试一次
                mpvDirectFailed = true
                scope.clientLog("engine-fallback", ["from": .string("mpv-direct"), "reason": .string(reason)])
                request(startMs: positionMs, phase: .sessionStarting)
                return
            }
            mpvFallback(reason: reason)
            return
        }
        if cause == .network {
            if networkRestarts.allowRestart() {
                // 取流持续失败（断线、token 过期、服务端中断）：同档原地重开（新会话 = 新 token），不降档；
                // 真断网时重开请求本身会失败，落到错误页（同 Web onNetworkDead）
                scope.clientLog("network-restart", ["reason": .string(reason), "attempt": .int(networkRestarts.consecutive)])
                request(startMs: positionMs, phase: .sessionStarting)
                return
            }
            // 连续重开都没能出画：「网络」归因多半不对，按这一档放不了继续往下走（降档或报错）
            scope.clientLog("network-restart-exhausted", ["reason": .string(reason)])
        }
        if engine?.kind == .avPlayer, cause == .decode, session.decision.video?.action != "transcode", mpvAvailable, !preferMPV {
            // 系统播放器在不重编码的档位放不出来（封装/编码细节不认）：交给 MPV 在本机直接放原文件，
            // 比让服务端降到转码档省事得多
            preferMPV = true
            scope.clientLog("engine-fallback", ["from": .string("avplayer"), "reason": .string(reason)])
            flash("系统播放器放不了这个文件，已改用 MPV")
            request(startMs: positionMs, phase: .sessionStarting)
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
            fail(reason, suggestion: "可以换一个版本重试；若反复出现，请打开「⋯ → 播放诊断」查看原因。")
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
        scrubFollowTask?.cancel()
        guard let engine, session != nil, phase != .sessionStarting, phase != .deciding, phase != .degrading else {
            // 会话正在重开的空档：改走换会话，新会话直接从目标位置起
            if phase.isBusy, session == nil, phase != .deciding || positionMs > 0 {
                positionMs = target
                request(startMs: target, phase: .sessionStarting)
            }
            return
        }
        if session?.timeline == "session", activeSessionId != nil, !withinSessionBuffer(target) {
            // 旧式会话相对列表只覆盖已转出的部分：落点在区间外才换会话，区间内原地跳（同 Web planSeek）
            positionMs = target
            request(startMs: target, phase: .sessionStarting)
            return
        }
        positionMs = target
        if phase == .ended { phase = .buffering }
        seekStartedAt = Date()
        stallWatch.reset()
        frameDrops.reset()
        engine.seek(to: Double(target - originMs) / 1000, exact: exact)
    }

    /// 落点是否在当前会话已转出的区间里（会话起点 ~ 已缓冲尾）
    private func withinSessionBuffer(_ fileMs: Int) -> Bool {
        guard let bufferedEndMs else { return false }
        return fileMs >= originMs && fileMs <= bufferedEndMs
    }

    // MARK: 拖动跟随

    /// 拖动进度条途中让画面跟着手指走（对应 Web scrub-follow.ts）：跳转便宜时（落点在缓冲里）10Hz 跟随，
    /// 原文件直出拖出缓冲时只在手指停住后跟一次，其余情况松手才跳。跟随不计入 seek 次数，松手那次才算
    func scrubFollow(toFileMs target: Int) {
        guard let engine, session != nil, [.playing, .buffering, .ended].contains(phase) else { return }
        let reachable = target >= originMs
        let cheap = target >= positionMs - 1000 && target <= (bufferedEndMs ?? 0)
        let now = Date()
        let plan = ScrubFollow.plan(
            nowMs: Int(now.timeIntervalSince1970 * 1000),
            lastFollowMs: Int(lastScrubFollowAt.timeIntervalSince1970 * 1000),
            cheap: cheap, reachable: reachable, settleOnly: playsOriginalFile
        )
        scrubFollowTask?.cancel()
        switch plan {
        case .skip:
            return
        case .follow:
            lastScrubFollowAt = now
            engine.seek(to: Double(target - originMs) / 1000, exact: false)
        case let .deferred(ms):
            scrubFollowTask = Task { [weak self] in
                try? await Task.sleep(for: .milliseconds(ms))
                guard let self, !Task.isCancelled, let engine = self.engine else { return }
                self.lastScrubFollowAt = Date()
                engine.seek(to: Double(target - self.originMs) / 1000, exact: false)
            }
        }
    }

    // MARK: 长按 2 倍速

    /// 现在能不能起长按倍速（同 Web canHoldSpeed：暂停时不行，松手按轻点处理）
    var canHoldSpeed: Bool { engine.map { !$0.isPaused } ?? false && phase == .playing }

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

    /// 画中画按钮显不显示：设备支持画中画就一直有——MPV 播放时点它会换成系统播放器再进画中画
    var pictureInPictureAvailable: Bool {
        guard let engine else { return false }
        if engine.supportsPictureInPicture { return true }
        return engine.kind == .mpv && engineOverride != .mpv && AVPictureInPictureController.isPictureInPictureSupported()
    }

    func togglePictureInPicture() {
        guard let engine else { return }
        if engine.supportsPictureInPicture {
            engine.togglePictureInPicture()
            return
        }
        guard pictureInPictureAvailable, !pendingPiP else { return }
        // MPV 没有画中画：在当前位置换成系统播放器（服务端换封装），就绪后自动进画中画。
        // 字幕按当前选择申请；图形字幕要压制而服务端没开软件转码时，由同意分支自动关掉字幕、不弹窗打断
        systemForPiP = true
        pendingPiP = true
        requestedSubtitle = selectedSubtitle ?? "off"
        wantsPlay = true
        flash("正在切换到系统播放器以开启画中画…")
        request(startMs: positionMs, phase: .sessionStarting)
    }

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

    /// 当前选中的字幕轨
    private var selectedOption: SubtitleOption? {
        selectedSubtitle.flatMap { ref in subtitles.options.first { $0.ref == ref } }
    }

    /// 当前字幕由引擎自己画：只有 MPV 画图形字幕（PGS）这一种。
    /// 文字字幕（SRT/ASS）两个引擎都交给叠加层用系统字体画——iOS 上 libass 用不了系统中文字体，
    /// mpv 自己画会变成方框或干脆不出字（模拟器与真机实测），样式设置也与系统播放器不一致
    var engineRendersSubtitles: Bool {
        guard let engine, let option = selectedOption else { return false }
        return engine.rendersSubtitle(kind: option.kind)
    }

    /// 选图形字幕要服务端压制进画面（系统播放器、且不能换 MPV 时），菜单里提前说明代价
    var graphicSubtitlesBurnIn: Bool { engine?.kind == .avPlayer && !mpvAvailable }

    func selectSubtitle(_ ref: String?) {
        subtitleTouched = true
        selectedSubtitle = ref
        let target = ref.flatMap { ref in subtitles.options.first { $0.ref == ref } }
        if engine?.kind == .mpv {
            // MPV：图形字幕交给 mpv 画，文字字幕由叠加层画（在 applySubtitleToEngine 里分流）
            applySubtitleToEngine()
            if burnedSubtitle != nil {
                // MPV 在放烧录过的转码流：撤下烧录
                requestedSubtitle = "off"
                request(startMs: positionMs, phase: .sessionStarting)
            }
            if reportedStart { sendProgress(paused: paused) }
            return
        }
        if shouldSwitchToMPV(forSubtitle: ref) {
            // 系统播放器上选了图形字幕：换 MPV 在本机画（不让服务端整片重新编码去压字幕）
            preferMPV = true
            requestedSubtitle = ref
            wantsPlay = true
            request(startMs: positionMs, phase: .sessionStarting)
            if reportedStart { sendProgress(paused: paused) }
            return
        }
        applySystemSubtitle()
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

    /// 当前是系统播放器、选中的是图形字幕（PGS）、MPV 可用 → 该换 MPV
    private func shouldSwitchToMPV(forSubtitle ref: String?) -> Bool {
        guard mpvAvailable, !preferMPV, engine?.kind != .mpv,
              let ref, let option = subtitles.options.first(where: { $0.ref == ref }) else { return false }
        return option.kind == "pgs"
    }

    /// 把当前字幕对应到 master 字幕组的下标，交给 AVPlayer 在画中画 / 隔空播放时由系统渲染。
    /// master 字幕组按会话字幕计划的顺序只收文本类（vtt/ass），与服务端 `_master_subtitle_tracks` 一致
    private func applySystemSubtitle() {
        guard let avPlayer = engine as? AVPlayerEngine else { return }
        guard session?.masterUrl != nil, session?.timeline == "file", burnedSubtitle == nil, let ref = selectedSubtitle,
              let plans = session?.decision.subtitles else {
            avPlayer.systemSubtitleIndex = nil
            return
        }
        let textPlans = plans.filter { ["vtt", "ass"].contains($0.kind) }
        avPlayer.systemSubtitleIndex = textPlans.firstIndex { $0.trackRef == ref }
    }

    /// 叠加层要渲染的文本字幕（两个引擎通用）：ASS 由服务端转成 VTT 纯文本。
    /// 隔空播放时字幕由系统画在电视上，本机叠加层收起
    var overlaySubtitleURL: URL? {
        guard !engineRendersSubtitles, burnedSubtitle == nil, !((engine as? AVPlayerEngine)?.systemSubtitlesActive ?? false),
              let ref = selectedSubtitle,
              let option = subtitles.options.first(where: { $0.ref == ref }), option.kind != "pgs" else { return nil }
        return scope.streamURL(option.path + "&format=vtt")
    }

    /// 把引擎自己画的字幕（MPV 的图形字幕）交给引擎；其余一律让引擎关掉字幕、由叠加层画
    private func applySubtitleToEngine() {
        guard let engine else { return }
        let own = selectedOption.flatMap { engine.rendersSubtitle(kind: $0.kind) ? $0 : nil }
        engine.selectSubtitle(own, url: own.flatMap { scope.streamURL($0.path) })
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

    // MARK: - 前后台

    func setBackgrounded(_ background: Bool) {
        backgrounded = background
        // 切后台先把当前位置报上去：之后 App 可能被挂起、被系统回收，等不到下一次心跳（同网页 visibilitychange）
        if background, reportedStart { sendProgress(paused: engine?.isPaused) }
        // 后台时引擎主动丢帧 / 不出画，回来先清窗口，免得误判卡顿与掉帧
        resetWatchdogs()
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
        let unit = self.unit, position = positionMs, audio = audioMemory, subtitle = subtitleMemory, duration = durationMs
        let scope = self.scope
        enqueueReport { [weak self] in
            let state = await scope.progress(unit, event: "progress", positionMs: position, durationMs: duration, paused: paused, audio: audio, subtitle: subtitle)
            self?.handleProgressResponse(state)
        }
    }

    private func enqueueReport(_ work: @escaping @MainActor () async -> Void) {
        let previous = reportQueue
        reportQueue = Task { @MainActor in
            await previous?.value
            await work()
        }
    }

    /// App 即将被结束：同步补发 stop（最多等 1.5 秒）
    private func reportTermination() {
        guard reportedStart else { return }
        reportedStart = false
        scope.stopBeforeTermination(unit, positionMs: positionMs, durationMs: durationMs, audio: audioMemory, subtitle: subtitleMemory)
    }

    /// 上报用的音轨记忆：刚换了音轨、新会话还没建好就退出时，也要记住用户的选择
    private var audioMemory: String? { requestedAudio ?? currentAudio }

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
        runWatchdogs(engine: engine, stats: stats)
        nowPlaying.updatePosition(controller: self)
    }

    private func resetWatchdogs() {
        stallWatch.reset()
        frameDrops.reset()
    }

    /// 每秒一次：卡顿归因（解码卡死 / 缺粮）与直通掉帧。命中就走既有的失败回路（降档 / 带宽重开 / MPV 回落）
    private func runWatchdogs(engine: any PlayerEngine, stats: EngineStats) {
        guard session != nil, [.buffering, .playing].contains(phase), !backgrounded else { return }
        if let grace = engine.watchdogGraceUntil, grace > Date() {
            // 引擎在做自身维护（重建视频输出）：清空窗口，宽限期过后重新开始采样
            resetWatchdogs()
            return
        }
        let starveLimit = playsOriginalFile ? StallWatch.directStarveSeconds : StallWatch.starveSeconds
        let ahead = max(0, (engine.bufferedEnd ?? engine.currentTime) - engine.currentTime)
        switch stallWatch.sample(time: engine.currentTime, bufferedAhead: ahead, paused: engine.isPaused,
                                 ended: phase == .ended, seeking: seekStartedAt != nil, starveLimit: starveLimit) {
        case .ok:
            break
        case .nudge:
            scope.clientLog("stall-nudge", ["position_ms": .int(positionMs)])
            engine.seek(to: engine.currentTime + StallWatch.nudgeStep, exact: true)
            engine.play()
        case .decodeStalled:
            engineFailed(reason: StallWatch.reason(.decodeStalled, starveLimit: starveLimit), cause: .decode)
            return
        case .starved:
            engineFailed(reason: StallWatch.reason(.starved, starveLimit: starveLimit), cause: .starved)
            return
        }
        // 掉帧只在视频直通时判：转码档已经是 h264，再掉帧说明连转码产物都放不动，继续降档只会更糟
        let copying = playsOriginalFile || session?.decision.video?.action == "copy"
        guard copying, phase == .playing, !engine.isPaused, seekStartedAt == nil,
              let dropped = stats.droppedFrames, let total = stats.totalFrames else { return }
        if let ratio = frameDrops.sample(dropped: dropped, total: total), ratio >= FrameDropTracker.ratio {
            frameDrops.reset()
            engineFailed(reason: "直通播放持续掉帧（\(Int((ratio * 100).rounded()))%），正在换转码重试", cause: .decode)
        }
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
                // 2 秒一次（同 Web）：诊断是旁路信息，不值得更密地打 NAS
                try? await Task.sleep(for: .seconds(2))
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

    /// 诊断面板「传输」节的 QoE 行：上次跳转耗时、卡顿次数与累计时长（卡顿不含 seek 造成的等待）
    var qoeLive: (lastSeekMs: Int?, rebufferCount: Int, rebufferMs: Int) {
        let ongoing = qoe.bufferingSince.map { Int(Date().timeIntervalSince($0) * 1000) } ?? 0
        return (qoe.lastSeekMs, qoe.rebufferCount, qoe.rebufferMs + ongoing)
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
    /// 最近一次 seek 从发出到重新出画的耗时（诊断面板「上次跳转 x 秒」）
    var lastSeekMs: Int?
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
