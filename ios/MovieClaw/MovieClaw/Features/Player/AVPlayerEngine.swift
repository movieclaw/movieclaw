import AVFoundation
import AVKit
import UIKit

/// 系统播放器引擎：AVPlayer 放服务端给的 MP4 直出地址或 HLS（fMP4）播放列表。
///
/// 为什么还要它（而不是全交给 MPV）：画中画、隔空播放（AirPlay 视频）、杜比视界与全景声透传
/// 只有 AVPlayer 能做；这些是 iPhone 上看片的日常刚需。它吃不下的容器/编码由服务端转封装或转码。
///
/// 字幕：AVPlayer 模式下由 SwiftUI 叠加层渲染（`SubtitleOverlay`），本引擎不碰字幕；
/// 图形字幕（PGS）走服务端烧录（重开会话），与网页端同一套规则。
@MainActor
final class AVPlayerEngine: NSObject, PlayerEngine {
    let kind = EngineKind.avPlayer
    var onEvent: ((EngineEvent) -> Void)?

    private let player = AVPlayer()
    private let layerView = PlayerLayerView()
    private var pipController: AVPictureInPictureController?
    private var observations: [NSKeyValueObservation] = []
    private var timeObserver: Any?
    private var notificationTokens: [NSObjectProtocol] = []
    private var watchdog: Timer?

    /// 起播目标（readyToPlay 之后才能 seek）
    private var pendingStart: Double = 0
    private var autoplay = true
    private var didPrepare = false
    private var ended = false
    private var bufferingSince: Date?
    private var desiredRate: Float = 1
    private(set) var isPictureInPictureActive = false

    /// 连续缓冲超过这个时长判为「供流中断」（交给控制器降档或按带宽重开）
    private static let stallLimit: TimeInterval = 45

    var view: UIView { layerView }

    override init() {
        super.init()
        layerView.playerLayer.player = player
        layerView.playerLayer.videoGravity = .resizeAspect
        player.allowsExternalPlayback = true
        player.usesExternalPlaybackWhileExternalScreenIsActive = true
        player.automaticallyWaitsToMinimizeStalling = true
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
        let event = player.currentItem?.accessLog()?.events.last
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
            droppedFrames: event.map(\.numberOfDroppedVideoFrames).flatMap { $0 >= 0 ? $0 : nil },
            totalFrames: nil,
            bufferedSeconds: max(0, (bufferedEnd ?? currentTime) - currentTime),
            currentTimeSeconds: currentTime,
            details: details
        )
    }

    // MARK: - 轨道与字幕（AVPlayer 模式下由控制器重开会话 / 叠加层渲染）

    var canSwitchAudioInPlace: Bool { false }
    func selectAudio(embeddedIndex: Int) {}
    var rendersSubtitles: Bool { false }
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

    /// 后台：不在画中画时把播放器从图层上摘下来，否则系统会连声音一起暂停
    func setBackgrounded(_ background: Bool) {
        guard !isPictureInPictureActive else { return }
        layerView.playerLayer.player = background ? nil : player
    }

    func destroy() {
        watchdog?.invalidate()
        watchdog = nil
        if let timeObserver { player.removeTimeObserver(timeObserver) }
        timeObserver = nil
        observations.removeAll()
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
        // 缓冲看门狗：连续缓冲太久判「供流中断」
        watchdog = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.checkStall() }
        }
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
                    self?.onEvent?(.failed(reason: Self.describe(error, fallback: "播放中断"), cause: .decode))
                }
            },
        ]
    }

    private func itemStatusChanged(_ status: AVPlayerItem.Status, error: Error?) {
        switch status {
        case .readyToPlay:
            guard !didPrepare else { return }
            didPrepare = true
            if pendingStart > 0.5 {
                let target = CMTime(seconds: pendingStart, preferredTimescale: 600)
                player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) { @Sendable [weak self] _ in
                    Task { @MainActor in self?.beginPlayback() }
                }
            } else {
                beginPlayback()
            }
        case .failed:
            onEvent?(.failed(reason: Self.describe(error, fallback: "系统播放器无法播放这个流"), cause: .decode))
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
            bufferingSince = nil
            onEvent?(.playing)
        case .paused:
            bufferingSince = nil
            if !ended { onEvent?(.paused) }
        case .waitingToPlayAtSpecifiedRate:
            if reason == .noItemToPlay { return }
            if bufferingSince == nil { bufferingSince = Date() }
            onEvent?(.buffering)
        @unknown default:
            break
        }
    }

    private func checkStall() {
        guard let since = bufferingSince, Date().timeIntervalSince(since) > Self.stallLimit else { return }
        bufferingSince = nil
        onEvent?(.failed(reason: "供流中断：缓冲超过 \(Int(Self.stallLimit)) 秒没有进展", cause: .starved))
    }

    private static func describe(_ error: Error?, fallback: String) -> String {
        guard let error = error as NSError? else { return fallback }
        let detail = error.localizedFailureReason ?? error.localizedDescription
        return "\(fallback)（\(detail)，代码 \(error.code)）"
    }
}

extension AVPlayerEngine: AVPictureInPictureControllerDelegate {
    nonisolated func pictureInPictureControllerDidStartPictureInPicture(_ controller: AVPictureInPictureController) {
        Task { @MainActor in
            self.isPictureInPictureActive = true
            self.onEvent?(.pictureInPicture(true))
        }
    }

    nonisolated func pictureInPictureControllerDidStopPictureInPicture(_ controller: AVPictureInPictureController) {
        Task { @MainActor in
            self.isPictureInPictureActive = false
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
