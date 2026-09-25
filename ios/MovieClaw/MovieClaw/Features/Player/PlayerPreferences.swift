import Foundation

/// 播放引擎偏好（播放器「⋯ 设置 → 播放引擎」，存本机）。
enum EnginePreference: String, CaseIterable, Identifiable {
    /// 服务端判定能原文件直出且 AVPlayer 吃得下 → 系统播放器；否则 MPV 直出
    case auto
    /// 始终用系统播放器（AVPlayer）：需要时由服务端转封装/转码
    case system
    /// 始终用 MPV（libmpv）：MKV/HEVC/TrueHD/DTS/ASS/PGS 都在本机解
    case mpv

    var id: String { rawValue }

    var label: String {
        switch self {
        case .auto: "自动"
        case .system: "系统播放器"
        case .mpv: "MPV"
        }
    }

    var hint: String {
        switch self {
        case .auto: "能直出用系统播放器，其余交给 MPV"
        case .system: "支持画中画、隔空播放、杜比视界"
        case .mpv: "本机解码 MKV/DTS/TrueHD，特效字幕原样渲染"
        }
    }
}

/// 字幕外观（对应 Web `lib/player/subtitles.ts` 的 SubtitleStyle）。
/// 时间轴偏移刻意不持久化：它是逐文件的修正，跨片带着只会错（同 Web）。
struct SubtitleStyle: Equatable, Codable {
    /// 相对视频高度的字号百分比（默认 5.2%）
    var fontScale: Double = 5.2
    /// 时间轴微调（秒），正数 = 字幕延后
    var offsetSeconds: Double = 0
    /// 距画面底部的百分比（默认 8%）
    var bottomPercent: Double = 8
    var outline: Bool = true
    var background: Bool = false

    enum CodingKeys: String, CodingKey { case fontScale, bottomPercent, outline, background }

    init() {}

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        fontScale = (try? container.decode(Double.self, forKey: .fontScale)) ?? 5.2
        bottomPercent = (try? container.decode(Double.self, forKey: .bottomPercent)) ?? 8
        outline = (try? container.decode(Bool.self, forKey: .outline)) ?? true
        background = (try? container.decode(Bool.self, forKey: .background)) ?? false
    }

    /// 时间轴微调步进：0.1 秒是人耳能分辨的最小对不齐量级
    static let offsetStep = 0.1

    /// 超过 ±30 秒基本不是「没对齐」而是拿错了字幕文件；并消掉浮点累加误差
    static func clampOffset(_ seconds: Double) -> Double {
        (min(30, max(-30, seconds)) * 10).rounded() / 10
    }
}

/// 播放器的本机偏好。键名带 `movieclaw.player.` 前缀，与 Web localStorage 的键同名同义。
enum PlayerPreferences {
    private static let defaults = UserDefaults.standard

    static var engine: EnginePreference {
        get { defaults.string(forKey: "movieclaw.player.engine").flatMap(EnginePreference.init) ?? .auto }
        set { defaults.set(newValue.rawValue, forKey: "movieclaw.player.engine") }
    }

    /// 画质上限（max_height）；nil = 自动
    static var quality: Int? {
        get {
            let value = defaults.integer(forKey: "movieclaw.player.quality")
            return QualityOption.all.contains { $0.maxHeight == value && value > 0 } ? value : nil
        }
        set {
            if let newValue { defaults.set(newValue, forKey: "movieclaw.player.quality") }
            else { defaults.removeObject(forKey: "movieclaw.player.quality") }
        }
    }

    static var subtitleStyle: SubtitleStyle {
        get {
            guard let data = defaults.data(forKey: "movieclaw.player.subtitle-style"),
                  let style = try? JSONDecoder().decode(SubtitleStyle.self, from: data) else { return SubtitleStyle() }
            return style
        }
        set {
            if let data = try? JSONEncoder().encode(newValue) {
                defaults.set(data, forKey: "movieclaw.player.subtitle-style")
            }
        }
    }

    /// 本机的播放设备标识（活动页「正在播放」按它区分会话；服务端会再加上成员命名空间）
    static var deviceId: String {
        if let stored = defaults.string(forKey: "movieclaw.player.device-id"),
           stored.range(of: "^[A-Za-z0-9_-]{8,64}$", options: .regularExpression) != nil {
            return stored
        }
        let generated = "ios-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(20).lowercased()
        defaults.set(generated, forKey: "movieclaw.player.device-id")
        return generated
    }
}

/// 画质档（对应 Web `lib/player/quality.ts`）。语义是**上限**：源不超所选档就照常直通。
struct QualityOption: Identifiable, Hashable {
    let maxHeight: Int?
    let label: String
    let hint: String

    var id: String { label }

    static let all: [QualityOption] = [
        QualityOption(maxHeight: nil, label: "自动", hint: "原画质优先，能直通不转码"),
        QualityOption(maxHeight: 1080, label: "1080p", hint: "约 6 Mbps"),
        QualityOption(maxHeight: 720, label: "720p", hint: "约 3 Mbps，网络一般时选它"),
        QualityOption(maxHeight: 480, label: "480p", hint: "约 1.5 Mbps，弱网救急"),
    ]
}

/// 分享访客的本机进度（对应 Web `lib/player/local-progress.ts`）：
/// 访客没有成员身份，进度不落服务端，只记在这台设备上，按分享 slug 分命名空间。
enum ShareLocalProgress {
    struct Record: Codable {
        var positionMs: Int
        var audioTrack: String?
        var subtitleTrack: String?
        var updatedAt: Double
    }

    private static func key(_ slug: String, _ unit: PlaybackUnit) -> String {
        "movieclaw.share.progress.\(slug).\(unit.mediaItemId).\(unit.season)x\(unit.episode)"
    }

    static func read(_ slug: String, _ unit: PlaybackUnit) -> Record? {
        guard let data = UserDefaults.standard.data(forKey: key(slug, unit)) else { return nil }
        return try? JSONDecoder().decode(Record.self, from: data)
    }

    static func write(_ slug: String, _ unit: PlaybackUnit, positionMs: Int?, audio: String?, subtitle: String?) {
        let previous = read(slug, unit)
        let record = Record(
            positionMs: max(0, positionMs ?? previous?.positionMs ?? 0),
            audioTrack: audio ?? previous?.audioTrack,
            subtitleTrack: subtitle ?? previous?.subtitleTrack,
            updatedAt: Date().timeIntervalSince1970
        )
        if let data = try? JSONEncoder().encode(record) {
            UserDefaults.standard.set(data, forKey: key(slug, unit))
        }
    }
}
