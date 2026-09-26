import AVFoundation
import AVKit
import UIKit

/// 系统播放器引擎：AVPlayer 放服务端给的 MP4 直出地址或 HLS（fMP4）播放列表。
///
/// 为什么还要它（而不是全交给 MPV）：画中画、隔空播放（AirPlay 视频）、杜比视界与全景声透传
/// 只有 AVPlayer 能做；这些是 iPhone 上看片的日常刚需。它吃不下的容器/编码由服务端转封装或转码。
///
/// 字幕：画面内由 SwiftUI 叠加层渲染（`SubtitleOverlay`，样式可调）；图形字幕（PGS）走服务端烧录。
/// 叠加层进不了画中画小窗和隔空播放的电视，所以放 VOD 转码流时吃服务端的 master 列表（带 WEBVTT 字幕组），
/// 进画中画 / 隔空播放时把当前字幕切成系统字幕轨由系统渲染，回到画面内再关掉（避免与叠加层双字幕）。
@MainActor
final class AVPlayerEngine: NSObject, PlayerEngine {
    let kind = EngineKind.avPlayer
    var onEvent: ((EngineEvent) -> Void)?

    private let player = AVPlayer()
    private let layerView = PlayerLayerView()
    private var pipController: AVPictureInPictureController?
    private var observations: [NSKeyValueObservation] = []
    /// 等画面就绪再进画中画（从 MPV 换过来时）
    private var pipPossibleObservation: NSKeyValueObservation?
    private var wantsPictureInPicture = false
    private var timeObserver: Any?
    private var notificationTokens: [NSObjectProtocol] = []

    /// 起播目标（readyToPlay 之后才能 seek）
    private var pendingStart: Double = 0
    private var autoplay = true
    private var didPrepare = false
    private var ended = false
    private var desiredRate: Float = 1
    private(set) var isPictureInPictureActive = false
    /// master 列表字幕组里对应当前字幕的下标（nil = 不选字幕 / 没有 master 字幕组）。
    /// 只在画中画、隔空播放时真正选中，画面内交给叠加层
    var systemSubtitleIndex: Int? {
        didSet { applySystemSubtitle() }
    }

    var view: UIView { layerView }

    override init() {
        super.init()
        layerView.playerLayer.player = player
        layerView.playerLayer.videoGravity = .resizeAspect
        player.allowsExternalPlayback = true
        player.usesExternalPlaybackWhileExternalScreenIsActive = true
        player.automaticallyWaitsToMinimizeStalling = true
        // 字幕轨由我们按「画面内 / 画中画」显式挑，不让系统按辅助功能偏好自动选（否则画面内会出双字幕）
        player.appliesMediaSelectionCriteriaAutomatically = false
        if AVPictureInPictureController.isPictureInPictureSupported() {
            let controller = AVPictureInPictureController(playerLayer: layerView.playerLayer)
            controller?.canStartPictureInPictureAutomaticallyFromInline = true
            controller?.delegate = self
            pipController = controller
        }
        observe()
    }

    // MARK: - 播放控制

    func load(url: URL, start: Double, autoplay: Bool) {
        let item = AVPlayerItem(url: url)
        pendingStart = start
        self.autoplay = autoplay
        didPrepare = false
        ended = false
        player.replaceCurrentItem(with: item)
        observeItem(item)
        onEvent?(.buffering)
    }

    func play() {
        if ended { ended = false }
        player.playImmediately(atRate: desiredRate)
    }

    func pause() { player.pause() }

    func seek(to seconds: Double, exact: Bool) {
        let time = CMTime(seconds: max(0, seconds), preferredTimescale: 600)
        let tolerance = exact ? CMTime.zero : CMTime(seconds: 2, preferredTimescale: 600)
        ended = false
        player.seek(to: time, toleranceBefore: tolerance, toleranceAfter: tolerance)
    }

    func setRate(_ rate: Float) {
        desiredRate = rate
        if player.rate > 0 { player.rate = rate }
    }

    // MARK: - 读数

    var currentTime: Double {
        let seconds = player.currentTime().seconds
        return seconds.isFinite ? max(0, seconds) : 0
    }

    var duration: Double? {
        guard let seconds = player.currentItem?.duration.seconds, seconds.isFinite, seconds > 0 else { return nil }
        return seconds
    }

    var bufferedEnd: Double? {
        let now = currentTime
        let ranges = player.currentItem?.loadedTimeRanges.map(\.timeRangeValue) ?? []
        let covering = ranges.first { $0.start.seconds <= now + 0.5 && $0.end.seconds >= now }
        return covering?.end.seconds ?? ranges.map(\.end.seconds).max()
    }

    var isPaused: Bool { player.timeControlStatus == .paused }

    var videoSize: CGSize { player.currentItem?.presentationSize ?? .zero }

    func stats() -> EngineStats {
        let events = player.currentItem?.accessLog()?.events ?? []
        let event = events.last
        let observed = event.map(\.observedBitrate).flatMap { $0 > 0 ? $0 : nil }
        let indicated = event.map(\.indicatedBitrate).flatMap { $0 > 0 ? $0 : nil }
            ?? event.map(\.averageVideoBitrate).flatMap { $0 > 0 ? $0 : nil }
        var details: [String] = []
        if player.isExternalPlaybackActive { details.append("隔空播放中") }
        if isPictureInPictureActive { details.append("画中画中") }
        return EngineStats(
            engine: kind.rawValue,
            downlinkBps: observed,
            bitrateBps: indicated,
            droppedFrames: events.isEmpty ? nil : events.reduce(0) { $0 + max(0, $1.numberOfDroppedVideoFrames) },
            totalFrames: estimatedTotalFrames(events),
            bufferedSeconds: max(0, (bufferedEnd ?? currentTime) - currentTime),
            currentTimeSeconds: currentTime,
            details: details
        )
    }

    /// AVPlayer 没有「已解码帧数」计数：按各段访问日志的观看时长 × 当前帧率估算，
    /// 供掉帧看门狗算窗口掉帧率（对应 Web 在 iOS 上用 webkitDecodedFrameCount 的做法）
    private func estimatedTotalFrames(_ events: [AVPlayerItemAccessLogEvent]) -> Int? {
        let fps = player.currentItem?.tracks.compactMap { $0.currentVideoFrameRate > 0 ? Double($0.currentVideoFrameRate) : nil }.first
        guard let fps, !events.isEmpty else { return nil }
        let watched = events.reduce(0.0) { $0 + max(0, $1.durationWatched) }
        return Int(watched * fps)
    }

    /// 画中画 / 隔空播放时选中 master 字幕组里的当前字幕，画面内一律不选
    private func applySystemSubtitle() {
        guard let item = player.currentItem else { return }
        let wantSystem = isPictureInPictureActive || player.isExternalPlaybackActive
        Task { @MainActor [weak self, weak item] in
            guard let item, let group = try? await item.asset.loadMediaSelectionGroup(for: .legible) else { return }
            guard let self, self.player.currentItem === item else { return }
            let option = wantSystem ? self.systemSubtitleIndex.flatMap { $0 < group.options.count ? group.options[$0] : nil } : nil
            item.select(option, in: group)
        }
    }

    /// 系统正在渲染字幕（隔空播放中）：叠加层此时画在一块黑的本机画面上，应当收起
    var systemSubtitlesActive: Bool { player.isExternalPlaybackActive && systemSubtitleIndex != nil }

    // MARK: - 轨道与字幕（AVPlayer 模式下由控制器重开会话 / 叠加层渲染）

    var canSwitchAudioInPlace: Bool { false }
    func selectAudio(embeddedIndex: Int) {}
    func rendersSubtitle(kind: String) -> Bool { false }
    func selectSubtitle(_ option: SubtitleOption?, url: URL?) {}
    func applySubtitleStyle(_ style: SubtitleStyle) {}

    // MARK: - 画中画

    var supportsPictureInPicture: Bool { pipController != nil }

    func togglePictureInPicture() {
        guard let pipController else { return }
        if pipController.isPictureInPictureActive {
            pipController.stopPictureInPicture()
        } else {
            pipController.startPictureInPicture()
        }
    }

    /// 画面就绪（`isPictureInPicturePossible`）后自动进画中画：MPV 播放中点画中画、换成系统播放器时用
    func startPictureInPictureWhenPossible() {
        guard let pipController, !pipController.isPictureInPictureActive else { return }
        if pipController.isPictureInPicturePossible {
            pipController.startPictureInPicture()
            return
        }
        wantsPictureInPicture = true
        pipPossibleObservation = pipController.observe(\.isPictureInPicturePossible, options: [.new]) { @Sendable [weak self] _, change in
            let possible = change.newValue ?? false
            Task { @MainActor in self?.pictureInPicturePossibleChanged(possible) }
        }
    }

    private func pictureInPicturePossibleChanged(_ possible: Bool) {
        guard possible, wantsPictureInPicture, let pipController, !pipController.isPictureInPictureActive else { return }
        wantsPictureInPicture = false
        pipPossibleObservation = nil
        pipController.startPictureInPicture()
    }

    /// 后台：不在画中画时把播放器从图层上摘下来，否则系统会连声音一起暂停
    func setBackgrounded(_ background: Bool) {
        guard !isPictureInPictureActive else { return }
        layerView.playerLayer.player = background ? nil : player
    }

    func destroy() {
        if let timeObserver { player.removeTimeObserver(timeObserver) }
        timeObserver = nil
        observations.removeAll()
        pipPossibleObservation = nil
        notificationTokens.forEach(NotificationCenter.default.removeObserver)
        notificationTokens.removeAll()
        pipController?.stopPictureInPicture()
        pipController = nil
        player.pause()
        player.replaceCurrentItem(with: nil)
        onEvent = nil
    }

    // MARK: - 观察

    private func observe() {
        observations.append(player.observe(\.timeControlStatus, options: [.new]) { @Sendable [weak self] player, _ in
            let status = player.timeControlStatus
            let reason = player.reasonForWaitingToPlay
            Task { @MainActor in self?.timeControlChanged(status, reason: reason) }
        })
        // 隔空播放进出：切换系统字幕轨（卡顿 / 缺粮由控制器的 StallWatch 统一判定）
        observations.append(player.observe(\.isExternalPlaybackActive, options: [.new]) { @Sendable [weak self] _, _ in
            Task { @MainActor in self?.applySystemSubtitle() }
        })
    }

    private func observeItem(_ item: AVPlayerItem) {
        observations.append(item.observe(\.status, options: [.new]) { @Sendable [weak self] item, _ in
            let status = item.status
            let error = item.error
            Task { @MainActor in self?.itemStatusChanged(status, error: error) }
        })
        let center = NotificationCenter.default
        notificationTokens.forEach(center.removeObserver)
        notificationTokens = [
            center.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification, object: item, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.ended = true
                    self?.onEvent?(.ended)
                }
            },
            center.addObserver(forName: AVPlayerItem.failedToPlayToEndTimeNotification, object: item, queue: .main) { [weak self] note in
                let error = note.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error
                MainActor.assumeIsolated {
                    self?.onEvent?(.failed(reason: Self.describe(error, fallback: "播放中断"), cause: Self.cause(of: error)))
                }
            },
        ]
    }

    private func itemStatusChanged(_ status: AVPlayerItem.Status, error: Error?) {
        switch status {
        case .readyToPlay:
            guard !didPrepare else { return }
            didPrepare = true
            applySystemSubtitle()
            if pendingStart > 0.5 {
                let target = CMTime(seconds: pendingStart, preferredTimescale: 600)
                player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) { @Sendable [weak self] _ in
                    Task { @MainActor in self?.beginPlayback() }
                }
            } else {
                beginPlayback()
            }
        case .failed:
            onEvent?(.failed(reason: Self.describe(error, fallback: "系统播放器无法播放这个流"), cause: Self.cause(of: error)))
        default:
            break
        }
    }

    /// 起播位置已就位：按需开始播放
    private func beginPlayback() {
        if autoplay { player.playImmediately(atRate: desiredRate) } else { onEvent?(.paused) }
    }

    private func timeControlChanged(_ status: AVPlayer.TimeControlStatus, reason: AVPlayer.WaitingReason?) {
        switch status {
        case .playing:
            onEvent?(.playing)
        case .paused:
            if !ended { onEvent?(.paused) }
        case .waitingToPlayAtSpecifiedRate:
            if reason == .noItemToPlay { return }
            onEvent?(.buffering)
        @unknown default:
            break
        }
    }

    /// 取流失败归因（对应 Web 的 onNetworkDead）：连接断开、超时、服务端中断这类「这一档没毛病、只是线没通」
    /// 的错误走同档原地重开（新会话 = 新 token），不降档；其余按「这一档放不了」降档
    static func cause(of error: Error?) -> EngineFailureCause {
        guard let error = error as NSError? else { return .decode }
        let chain = [error] + [error.userInfo[NSUnderlyingErrorKey] as? NSError].compactMap { $0 }
        for item in chain {
            if item.domain == NSURLErrorDomain { return .network }
            // -11863 资源不可用 / -11800 且底层是网络错误 / -12938 HTTP 4xx / -12660 HTTP 403。
            // 注意 -11828 是 AVErrorFileFormatNotRecognized（格式无法识别）：这一档本身放不了，必须走降档，
            // 当成网络错误会同档无限重开（第二轮审计 N-05-1）
            if item.domain == AVFoundationErrorDomain, item.code == -11863 { return .network }
            if item.domain == "CoreMediaErrorDomain", [-12938, -12660, -12971, -12645, -12889].contains(item.code) { return .network }
        }
        return .decode
    }

    private static func describe(_ error: Error?, fallback: String) -> String {
        guard let error = error as NSError? else { return fallback }
        let detail = error.localizedFailureReason ?? error.localizedDescription
        return "\(fallback)（\(detail)，代码 \(error.code)）"
    }
}

extension AVPlayerEngine: AVPictureInPictureControllerDelegate {
    /// 进画中画之前就切好系统字幕轨，小窗一出来就带着字幕
    nonisolated func pictureInPictureControllerWillStartPictureInPicture(_ controller: AVPictureInPictureController) {
        Task { @MainActor in
            self.isPictureInPictureActive = true
            self.applySystemSubtitle()
        }
    }

    nonisolated func pictureInPictureControllerDidStartPictureInPicture(_ controller: AVPictureInPictureController) {
        Task { @MainActor in
            self.isPictureInPictureActive = true
            self.onEvent?(.pictureInPicture(true))
        }
    }

    nonisolated func pictureInPictureControllerDidStopPictureInPicture(_ controller: AVPictureInPictureController) {
        Task { @MainActor in
            self.isPictureInPictureActive = false
            self.applySystemSubtitle()
            self.onEvent?(.pictureInPicture(false))
        }
    }

    nonisolated func pictureInPictureController(
        _ controller: AVPictureInPictureController,
        restoreUserInterfaceForPictureInPictureStopWithCompletionHandler completionHandler: @escaping (Bool) -> Void
    ) {
        // 播放器界面一直在（全屏覆盖层没有关），直接告诉系统可以还原
        completionHandler(true)
    }
}

/// 以 AVPlayerLayer 为底层图层的视图（尺寸随布局自动跟随）
final class PlayerLayerView: UIView {
    override static var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
}
