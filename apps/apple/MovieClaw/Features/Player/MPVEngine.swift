import MPVCore
import UIKit

/// MPV 引擎：通过 MPVCore（libmpv + FFmpeg 的 LGPL 动态框架）播放。
///
/// 两种用法（由控制器决定，`playsOriginalFile` 标明）：
/// - **原文件直出**：服务端判定视频可直通时，直接拉 `/playback/files/{id}/stream` 原文件——
///   MKV/HEVC/TrueHD/DTS 都在本机解，服务端零开销；内封音轨可原地切换，图形字幕（PGS）由 mpv 原样画；
/// - **放服务端 HLS**：需要转码（画质上限、带宽不足）时照样能放 fMP4 播放列表，此时字幕按地址外挂。
///
/// 文字字幕（SRT/ASS）不由 mpv 画，与系统播放器一样交给 SwiftUI 叠加层用系统字体画：iOS 上 libass
/// 用不了系统的中文字体（苹方能按名字找到，却画不出字形），中文会变成方框或干脆不出字（模拟器与真机实测）。
/// 字幕样式映射到 mpv 选项（只剩图形字幕的位置会用到）：mpv 的字号/边距以「720 像素高的窗口」为基准，
/// 所以网页的「画面高度百分比」× 720 就是对应的 mpv 数值。
@MainActor
final class MPVEngine: PlayerEngine {
    let kind = EngineKind.mpv
    var onEvent: ((EngineEvent) -> Void)?

    /// 正在直出原文件（而不是服务端 HLS）：内封轨可原地切换
    let playsOriginalFile: Bool

    private let core: MPVPlayer
    private var fileLoaded = false
    private var paused = true
    private var pausedForCache = false
    private var seeking = false
    private var eofReached = false
    private var time: Double = 0
    private var totalDuration: Double?
    private var cacheTime: Double?
    private var width = 0
    private var height = 0
    private var lastReported: EngineEvent?

    /// 文件装载前就选定的轨，装载后补上
    private var pendingSubtitle: (option: SubtitleOption?, url: URL?)?
    private var pendingAudio: Int?
    /// 已经 sub-add 过的外挂轨（轨引用 → 是否已挂上），避免重复下载
    private var addedSubtitles: Set<String> = []

    var view: UIView { core.view }

    init(playsOriginalFile: Bool) throws {
        self.playsOriginalFile = playsOriginalFile
        #if DEBUG
        // 开发期：-mcMPVBackend metal|openGL 强制渲染方式（验证模拟器上 MoltenVK 是否可用）
        let forced = UserDefaults.standard.string(forKey: "mcMPVBackend").flatMap(MPVRenderBackend.init(rawValue:))
        core = try MPVPlayer(backend: forced)
        #else
        core = try MPVPlayer()
        #endif
        // 卡顿 / 缺粮由控制器的 StallWatch 按播放头与缓冲统一判定（两个引擎同一套口径）
        core.onEvent = { [weak self] event in self?.handle(event) }
        // 旋转后重建视频输出：4K HEVC 解码重启要几秒，期间掉帧与画面停顿是预期内的，
        // 不能让掉帧看门狗判成「直通放不动」而回落换引擎（真机《抓特务》实测会被连环重启）
        core.onVideoOutputRebuild = { [weak self] in
            self?.watchdogGraceUntil = Date().addingTimeInterval(6)
        }
    }

    private(set) var watchdogGraceUntil: Date?

    /// 渲染方式（诊断面板用）
    var renderBackend: String {
        switch core.backend {
        case .metal: "Metal（MoltenVK · gpu-next）"
        case .openGL: "OpenGL ES（libmpv render API）"
        }
    }

    // MARK: - 播放控制

    func load(url: URL, start: Double, autoplay: Bool) {
        fileLoaded = false
        eofReached = false
        paused = !autoplay
        addedSubtitles.removeAll()
        core.load(url, start: start > 0.5 ? start : nil, paused: !autoplay)
        emit(.buffering)
    }

    func play() {
        if eofReached { eofReached = false }
        core.play()
    }

    func pause() { core.pause() }

    func seek(to seconds: Double, exact: Bool) {
        eofReached = false
        core.seek(to: seconds, exact: exact)
    }

    func setRate(_ rate: Float) { core.setSpeed(Double(rate)) }

    // MARK: - 读数

    var currentTime: Double { time }
    var duration: Double? { totalDuration }
    var bufferedEnd: Double? { cacheTime }
    var isPaused: Bool { paused }
    var videoSize: CGSize { CGSize(width: width, height: height) }

    /// 诊断读数。只读上一秒后台采样的结果（`MPVPlayer.sampleReadouts`），绝不在主线程同步读 mpv 属性——
    /// 核心线程忙时同步读会把主线程一起卡住（界面无响应、严重时被系统杀掉）
    func stats() -> EngineStats {
        core.sampleReadouts(
            doubles: ["cache-speed", "video-bitrate", "audio-bitrate"],
            ints: ["frame-drop-count", "decoder-frame-drop-count", "estimated-frame-number", "audio-params/channel-count"],
            strings: ["video-codec", "hwdec-current", "audio-codec-name"]
        )
        let readouts = core.readouts
        let cacheSpeed = readouts.doubles["cache-speed"].map { $0 * 8 }
        let videoBitrate = readouts.doubles["video-bitrate"] ?? 0
        let audioBitrate = readouts.doubles["audio-bitrate"] ?? 0
        let dropped = (readouts.ints["frame-drop-count"] ?? 0) + (readouts.ints["decoder-frame-drop-count"] ?? 0)
        var details = ["渲染 \(renderBackend)"]
        let decoder = [readouts.strings["video-codec"], readouts.strings["hwdec-current"].map { $0 == "no" ? "软件解码" : "硬件解码 \($0)" }]
            .compactMap { $0 }.filter { !$0.isEmpty }
        if !decoder.isEmpty { details.append("视频 " + decoder.joined(separator: " · ")) }
        if let audio = readouts.strings["audio-codec-name"], !audio.isEmpty {
            details.append("音频 \(audio)" + (readouts.ints["audio-params/channel-count"].map { " · \($0) 声道" } ?? ""))
        }
        details.append(playsOriginalFile ? "直出原文件" : "播放服务端 HLS")
        return EngineStats(
            engine: kind.rawValue,
            downlinkBps: cacheSpeed.flatMap { $0 > 0 ? $0 : nil },
            bitrateBps: videoBitrate + audioBitrate > 0 ? videoBitrate + audioBitrate : nil,
            droppedFrames: dropped,
            totalFrames: readouts.ints["estimated-frame-number"],
            bufferedSeconds: max(0, (cacheTime ?? time) - time),
            currentTimeSeconds: time,
            details: details
        )
    }

    // MARK: - 音轨

    var canSwitchAudioInPlace: Bool { playsOriginalFile }

    func selectAudio(embeddedIndex: Int) {
        guard fileLoaded else { pendingAudio = embeddedIndex; return }
        let audio = core.tracks().filter { $0.type == "audio" && !$0.isExternal }.sorted { $0.id < $1.id }
        guard embeddedIndex < audio.count else { return }
        core.setString("aid", String(audio[embeddedIndex].id))
    }

    // MARK: - 字幕（mpv 只画图形字幕 PGS，文字字幕交给叠加层）

    func rendersSubtitle(kind: String) -> Bool { kind == "pgs" }

    func selectSubtitle(_ option: SubtitleOption?, url: URL?) {
        guard fileLoaded else { pendingSubtitle = (option, url); return }
        guard let option else {
            core.setString("sid", "no")
            return
        }
        if playsOriginalFile, let index = option.embeddedIndex {
            // 直出原文件：内封轨就在容器里，按同类型顺序对位（embedded:N = 第 N 条内封字幕）
            let embedded = core.tracks().filter { $0.type == "sub" && !$0.isExternal }.sorted { $0.id < $1.id }
            if index < embedded.count {
                core.setString("sid", String(embedded[index].id))
                return
            }
        }
        if addedSubtitles.contains(option.ref),
           let track = core.tracks().first(where: { $0.type == "sub" && $0.isExternal && $0.title == option.ref }) {
            core.setString("sid", String(track.id))
            return
        }
        guard let url else { return }
        // 服务端地址按轨引用命名，下次切回来直接复用已挂上的轨
        addedSubtitles.insert(option.ref)
        core.command(["sub-add", url.absoluteString, "select", option.ref])
    }

    func applySubtitleStyle(_ style: SubtitleStyle) {
        core.setDouble("sub-delay", style.offsetSeconds)
        core.setDouble("sub-font-size", style.fontScale / 100 * 720)
        core.setDouble("sub-margin-y", style.bottomPercent / 100 * 720)
        core.setDouble("sub-border-size", style.outline ? 2.5 : 0)
        core.setString("sub-border-style", style.background ? "background-box" : "outline-and-shadow")
        core.setString("sub-back-color", "#99000000")
        core.setDouble("sub-shadow-offset", style.outline ? 1 : 0)
    }

    // MARK: - 画中画 / 前后台

    var supportsPictureInPicture: Bool { false }
    var isPictureInPictureActive: Bool { false }
    func togglePictureInPicture() {}

    func setBackgrounded(_ background: Bool) {
        core.setVideoOutputEnabled(!background)
    }

    func destroy() {
        onEvent = nil
        core.destroy()
    }

    // MARK: - 事件

    private func handle(_ event: MPVEvent) {
        switch event {
        case .fileLoaded:
            fileLoaded = true
            if let pendingAudio { selectAudio(embeddedIndex: pendingAudio) }
            pendingAudio = nil
            if let pendingSubtitle { selectSubtitle(pendingSubtitle.option, url: pendingSubtitle.url) }
            pendingSubtitle = nil
        case .playbackRestart:
            seeking = false
            reportState()
        case let .endFile(reason, error):
            if reason == "error" {
                emit(.failed(reason: "MPV 无法播放这个文件（\(error ?? "未知错误")）", cause: .decode))
            } else if reason == "eof" {
                eofReached = true
                emit(.ended)
            }
        case let .property(name, value):
            apply(name, value)
        case .log:
            break
        }
    }

    private func apply(_ name: String, _ value: MPVValue) {
        switch (name, value) {
        case let ("time-pos", .double(seconds)): time = seconds
        case let ("duration", .double(seconds)): totalDuration = seconds > 0 ? seconds : nil
        case let ("demuxer-cache-time", .double(seconds)): cacheTime = seconds
        case let ("dwidth", .int(value)): width = Int(value)
        case let ("dheight", .int(value)): height = Int(value)
        case let ("pause", .flag(flag)):
            paused = flag
            reportState()
        case let ("paused-for-cache", .flag(flag)):
            pausedForCache = flag
            reportState()
        case let ("seeking", .flag(flag)):
            seeking = flag
            reportState()
        case let ("eof-reached", .flag(flag)):
            if flag, !eofReached {
                eofReached = true
                emit(.ended)
            } else if !flag {
                eofReached = false
            }
        default:
            break
        }
    }

    /// 把 mpv 的几个布尔状态归约成控制器关心的三态（只在变化时上报）
    private func reportState() {
        guard fileLoaded, !eofReached else { return }
        if pausedForCache || seeking {
            emit(.buffering)
        } else if paused {
            emit(.paused)
        } else {
            emit(.playing)
        }
    }

    private func emit(_ event: EngineEvent) {
        switch (event, lastReported) {
        case (.playing, .playing?), (.paused, .paused?), (.buffering, .buffering?):
            return
        default:
            lastReported = event
            onEvent?(event)
        }
    }
}
