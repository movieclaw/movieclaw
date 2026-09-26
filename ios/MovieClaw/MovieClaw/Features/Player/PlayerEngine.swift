import UIKit

/// 实际在跑的播放引擎。
enum EngineKind: String {
    /// AVPlayer：HLS / MP4；画中画、隔空播放、杜比视界、全景声
    case avPlayer = "avplayer"
    /// libmpv（MPVKit LGPL 构建）：MKV/HEVC/TrueHD/DTS 直出，ASS/PGS 由 libass 渲染
    case mpv

    var label: String {
        switch self {
        case .avPlayer: "系统播放器（AVPlayer）"
        case .mpv: "MPV（libmpv）"
        }
    }
}

/// 引擎失败的归因（对应 Web engine.ts 的 cause）：缺粮与解码卡死的处置完全不同——
/// 缺粮可能是带宽不够（该压码率），解码卡死才是这一档放不了（该降档）。
enum EngineFailureCause {
    case starved
    case decode
    /// 取流失败（断线、超时、token 过期、服务端中断）：这一档没毛病，同档原地重开，不降档
    case network
}

/// 引擎 → 控制器的事件。时间类读数不走事件，由控制器按需读 `currentTime` 等属性。
enum EngineEvent {
    /// 开始出画 / 恢复播放
    case playing
    case paused
    /// 缓冲中（起播、seek 后、缺粮）
    case buffering
    case ended
    case failed(reason: String, cause: EngineFailureCause)
    /// 画中画进出
    case pictureInPicture(Bool)
}

/// 引擎的实时读数（诊断面板「传输」与遥测用）
struct EngineStats {
    var engine: String
    /// 取流速度（bps）；样本不够时为 nil
    var downlinkBps: Double?
    /// 当前流的码率（bps）
    var bitrateBps: Double?
    var droppedFrames: Int?
    var totalFrames: Int?
    var bufferedSeconds: Double
    var currentTimeSeconds: Double
    /// 额外的引擎细节行（解码器、渲染方式等）
    var details: [String] = []
}

/// 播放引擎抽象（Swiftfin 式多引擎，docs/design/ios-app.md §4）。
///
/// 控制器只和这层打交道：会话协议、降档、进度上报与引擎无关；
/// 引擎只管「把这个地址放出来」并如实报告状态。时间一律是**流时间**（秒），
/// 与文件时间的换算（会话时间轴起点）由控制器负责。
@MainActor
protocol PlayerEngine: AnyObject {
    var kind: EngineKind { get }
    /// 渲染表面（铺满播放区域）
    var view: UIView { get }
    var onEvent: ((EngineEvent) -> Void)? { get set }

    /// 装载并（按需）起播；start 为流时间秒数
    func load(url: URL, start: Double, autoplay: Bool)
    func play()
    func pause()
    /// exact：精确定位（拖进度条松手）；否则允许按关键帧（±10 秒这类高频操作更快）
    func seek(to seconds: Double, exact: Bool)
    func setRate(_ rate: Float)

    var currentTime: Double { get }
    var duration: Double? { get }
    /// 已缓冲到的流时间
    var bufferedEnd: Double? { get }
    var isPaused: Bool { get }
    var videoSize: CGSize { get }
    func stats() -> EngineStats
    /// 引擎在做自身的维护动作（MPV 旋转后重建视频输出）：到这个时间点之前看门狗不判卡顿/掉帧
    var watchdogGraceUntil: Date? { get }

    /// 能否原地换音轨（MPV 直出原文件时可以；HLS/AVPlayer 下要重开会话）
    var canSwitchAudioInPlace: Bool { get }
    func selectAudio(embeddedIndex: Int)

    /// 字幕由引擎自己渲染（MPV）；否则由 SwiftUI 叠加层渲染（AVPlayer）
    var rendersSubtitles: Bool { get }
    /// 选字幕：embeddedIndex 非空且直出原文件时直接选内封轨，否则挂服务端地址
    func selectSubtitle(_ option: SubtitleOption?, url: URL?)
    func applySubtitleStyle(_ style: SubtitleStyle)

    var supportsPictureInPicture: Bool { get }
    var isPictureInPictureActive: Bool { get }
    func togglePictureInPicture()

    /// App 前后台切换（后台只留声音）
    func setBackgrounded(_ background: Bool)
    func destroy()
}

extension PlayerEngine {
    var watchdogGraceUntil: Date? { nil }
}
