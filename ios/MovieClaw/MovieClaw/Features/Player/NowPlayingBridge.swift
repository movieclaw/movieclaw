import MediaPlayer
import UIKit

/// 锁屏 / 控制中心（对应 Web 的 navigator.mediaSession）。
///
/// 两个引擎都不会自动发布「正在播放」信息（AVPlayer 只有交给 AVPlayerViewController 才会），
/// 所以统一由这里维护：标题、季集、海报、时长、进度、速率；远程命令支持播放/暂停、±10 秒、
/// 拖动进度、上一集/下一集（有才启用）。
@MainActor
final class NowPlayingBridge {
    private weak var controller: PlaybackController?
    private var targets: [(MPRemoteCommand, Any)] = []
    private var artworkURL: URL?
    private var artwork: MPMediaItemArtwork?

    func attach(to controller: PlaybackController) {
        self.controller = controller
        let center = MPRemoteCommandCenter.shared()
        center.skipForwardCommand.preferredIntervals = [10]
        center.skipBackwardCommand.preferredIntervals = [10]
        add(center.playCommand) { $0.play() }
        add(center.pauseCommand) { $0.pause() }
        add(center.togglePlayPauseCommand) { $0.togglePlay() }
        add(center.skipForwardCommand) { $0.seek(by: 10) }
        add(center.skipBackwardCommand) { $0.seek(by: -10) }
        add(center.nextTrackCommand) { $0.playNext() }
        add(center.previousTrackCommand) { $0.playPrevious() }
        let target = center.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            let seconds = event.positionTime
            MainActor.assumeIsolated { self?.controller?.seek(toFileMs: Int(seconds * 1000)) }
            return .success
        }
        targets.append((center.changePlaybackPositionCommand, target))
    }

    private func add(_ command: MPRemoteCommand, _ action: @escaping @MainActor (PlaybackController) -> Void) {
        let target = command.addTarget { [weak self] _ in
            MainActor.assumeIsolated {
                guard let controller = self?.controller else { return .noActionableNowPlayingItem }
                action(controller)
                return .success
            }
        }
        targets.append((command, target))
    }

    func detach() {
        for (command, target) in targets { command.removeTarget(target) }
        targets.removeAll()
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        MPNowPlayingInfoCenter.default().playbackState = .stopped
    }

    /// 条目/单元变化：刷新静态信息（标题、季集、海报）与上一集/下一集的可用性
    func update(controller: PlaybackController) {
        let center = MPRemoteCommandCenter.shared()
        center.nextTrackCommand.isEnabled = controller.nextEpisode != nil
        center.previousTrackCommand.isEnabled = controller.previousEpisode != nil
        var info = MPNowPlayingInfoCenter.default().nowPlayingInfo ?? [:]
        info[MPMediaItemPropertyTitle] = controller.title
        info[MPMediaItemPropertyArtist] = controller.episodeLabel(controller.currentEpisode) ?? controller.info?.year.map(String.init) ?? ""
        info[MPNowPlayingInfoPropertyMediaType] = MPNowPlayingInfoMediaType.video.rawValue
        if let artwork { info[MPMediaItemPropertyArtwork] = artwork }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
        updatePosition(controller: controller)
        loadArtwork(controller)
    }

    /// 每秒一次：进度、时长、速率
    func updatePosition(controller: PlaybackController) {
        var info = MPNowPlayingInfoCenter.default().nowPlayingInfo ?? [:]
        info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = Double(controller.positionMs) / 1000
        if let duration = controller.durationMs { info[MPMediaItemPropertyPlaybackDuration] = Double(duration) / 1000 }
        let rate = controller.paused ? 0.0 : (controller.holdSpeedActive ? 2.0 : 1.0)
        info[MPNowPlayingInfoPropertyPlaybackRate] = rate
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
        MPNowPlayingInfoCenter.default().playbackState = controller.paused ? .paused : .playing
    }

    /// 系统可能在任意线程回调取图闭包：必须在非隔离上下文里创建，免得闭包被推断成主线程隔离
    nonisolated private static func makeArtwork(_ image: UIImage) -> MPMediaItemArtwork {
        MPMediaItemArtwork(boundsSize: image.size) { _ in image }
    }

    private func loadArtwork(_ controller: PlaybackController) {
        guard let url = controller.scope.api.image(controller.info?.posterUrl), url != artworkURL else { return }
        artworkURL = url
        let session = controller.scope.api.session
        Task { [weak self, weak controller] in
            guard let (data, _) = try? await session.data(from: url), let image = UIImage(data: data) else { return }
            self?.artwork = Self.makeArtwork(image)
            if let controller { self?.update(controller: controller) }
        }
    }
}
