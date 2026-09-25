import AVFoundation
import UIKit
import VideoToolbox

/// 客户端解码能力快照（字段与后端 ClientCapabilityIn 严格对应，参考 Web `lib/player/capability.ts`）。
///
/// **按当前引擎如实申报**——服务端拿它决定档位，报多了会把放不了的原片直通给我们，报少了白白转码：
/// - AVPlayer：只报 VideoToolbox 真能解、AVFoundation 真能封装的编码；容器只认 MP4 与 HLS fMP4；
/// - MPV：libmpv 自带 FFmpeg 软解兜底，能力远大于网页——HEVC/AV1/VP9/VC-1/MPEG-2，
///   TrueHD/DTS/FLAC/Opus 全都能直接放；HDR 由 mpv 自己做色调映射，不需要服务端 tone-map。
///
/// codec 用归一化的编码家族名（h264 / hevc / aac …），服务端直接和 ffprobe 的 codec_name 比对。
enum PlayerCapability {
    /// 系统播放器的能力
    static func avPlayer() -> API.ClientCapabilityIn {
        var video: [API.VideoSupportIn] = [
            .init(codec: "h264", maxHeight: 2160, smooth: true, powerEfficient: true),
            .init(codec: "hevc", maxHeight: 2160, smooth: true, powerEfficient: VTIsHardwareDecodeSupported(kCMVideoCodecType_HEVC)),
        ]
        // AV1 只有 A17 Pro / M 系列之后才有硬解；软解 4K AV1 在手机上放不动，不报
        if VTIsHardwareDecodeSupported(kCMVideoCodecType_AV1) {
            video.append(.init(codec: "av1", maxHeight: 2160, smooth: true, powerEfficient: true))
        }
        let audio: [API.AudioSupportIn] = [
            .init(codec: "aac", maxChannels: 8),
            .init(codec: "ac3", maxChannels: 6),
            // E-AC-3 含杜比全景声（JOC）：AVPlayer 在支持的输出设备上原样透传
            .init(codec: "eac3", maxChannels: 8),
            .init(codec: "flac", maxChannels: 8),
            .init(codec: "alac", maxChannels: 8),
            .init(codec: "mp3", maxChannels: 2),
        ]
        return API.ClientCapabilityIn(
            video: video,
            audio: audio,
            containers: ["mp4", "hls-fmp4"],
            hdrPassthrough: hdrDisplay,
            mse: "none",
            isMobile: UIDevice.current.userInterfaceIdiom == .phone,
            nativeHls: true
        )
    }

    /// MPV 的能力：能解就报，不看硬解（FFmpeg 软解兜底）
    static func mpv() -> API.ClientCapabilityIn {
        let video = ["h264", "hevc", "av1", "vp9", "vp8", "mpeg2video", "mpeg4", "vc1"].map {
            API.VideoSupportIn(codec: $0, maxHeight: 2160, smooth: true, powerEfficient: $0 == "h264" || $0 == "hevc")
        }
        let audio = ["aac", "ac3", "eac3", "truehd", "dts", "flac", "alac", "opus", "vorbis", "mp3", "mp2", "pcm_s16le", "pcm_s24le"].map {
            API.AudioSupportIn(codec: $0, maxChannels: 8)
        }
        return API.ClientCapabilityIn(
            video: video,
            audio: audio,
            containers: ["mp4", "hls-fmp4", "mkv", "webm", "ts", "m2ts", "avi"],
            // mpv 自己做 HDR→SDR 色调映射，服务端不需要 tone-map
            hdrPassthrough: true,
            mse: "none",
            isMobile: UIDevice.current.userInterfaceIdiom == .phone,
            nativeHls: true
        )
    }

    /// 屏幕能不能显示 HDR（能力快照的 hdr_passthrough）；判 false 只是让服务端 tone-map
    private static var hdrDisplay: Bool {
        let screen = UIApplication.shared.connectedScenes.compactMap { ($0 as? UIWindowScene)?.screen }.first
        return (screen?.potentialEDRHeadroom ?? 1) > 1
    }
}
