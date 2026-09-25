import Foundation

/// 语言代码 → 中文名（对应 Web `lib/language-labels.ts`，同一条轨在详情页与播放器里叫同一个名字）。
enum LanguageLabel {
    private static let labels: [String: String] = [
        "chs": "简体中文", "cht": "繁体中文", "chi": "中文", "zho": "中文", "cmn": "中文",
        "yue": "粤语", "eng": "英语", "jpn": "日语", "kor": "韩语", "fre": "法语", "fra": "法语",
        "ger": "德语", "deu": "德语", "spa": "西班牙语", "rus": "俄语", "ita": "意大利语",
        "por": "葡萄牙语", "tha": "泰语", "hin": "印地语",
    ]

    /// 未知语言（und）与空值返回 nil，由调用方决定占位文案
    static func of(_ code: String?) -> String? {
        guard let code, !code.isEmpty, code != "und" else { return nil }
        return labels[code.lowercased()] ?? code
    }
}

/// 字幕菜单里的一条可选轨（对应 Web `SubtitleOption`）。
struct SubtitleOption: Identifiable, Hashable {
    /// 中性轨引用（embedded:N / external:文件名），同时用作轨记忆的值
    let ref: String
    let label: String
    /// vtt（文本）/ ass（特效）/ pgs（图形）
    let kind: String
    /// 服务端地址（已带签名 token，原格式）
    let path: String
    let language: String?
    let isDefault: Bool
    let isAI: Bool

    var id: String { ref }

    /// 内封轨的数组下标（embedded:N → N）；外挂轨为 nil
    var embeddedIndex: Int? { ref.hasPrefix("embedded:") ? Int(ref.dropFirst("embedded:".count)) : nil }
}

/// 拿不到的轨（连同中文原因），菜单里置灰展示而不是给一个点了没反应的选项。
struct UnavailableSubtitle: Identifiable, Hashable {
    let ref: String
    let label: String
    let reason: String
    var id: String { ref }
}

struct SubtitleTracks: Equatable {
    var options: [SubtitleOption] = []
    var unavailable: [UnavailableSubtitle] = []

    /// 把决策里的字幕计划配上取流地址（与 subtitle_urls 严格一一对应，少一个就当那条没有地址）
    static func plan(_ plans: [API.SubtitlePlanView], urls: [String]) -> SubtitleTracks {
        var result = SubtitleTracks()
        for (index, plan) in plans.enumerated() {
            let label = trackLabel(plan)
            guard index < urls.count else {
                result.unavailable.append(.init(ref: plan.trackRef, label: label, reason: "服务端没有给出这条轨的地址"))
                continue
            }
            guard ["vtt", "ass", "pgs"].contains(plan.kind) else {
                result.unavailable.append(.init(ref: plan.trackRef, label: label, reason: "暂不支持的字幕格式：\(plan.kind)"))
                continue
            }
            result.options.append(SubtitleOption(
                ref: plan.trackRef, label: label, kind: plan.kind, path: urls[index],
                language: plan.language, isDefault: plan.isDefault, isAI: plan.isAi
            ))
        }
        return result
    }

    private static let kindLabels = ["vtt": "文本", "ass": "特效", "pgs": "图形"]

    private static func trackLabel(_ plan: API.SubtitlePlanView) -> String {
        let name = LanguageLabel.of(plan.language) ?? refLabel(plan.trackRef)
        return "\(name) · \(kindLabels[plan.kind] ?? plan.kind)"
    }

    /// 没有语言标记时的兜底名：外挂轨用文件名，内封轨用序号
    private static func refLabel(_ ref: String) -> String {
        if ref.hasPrefix("external:") { return String(ref.dropFirst("external:".count)) }
        if ref.hasPrefix("embedded:") { return "内封轨 \(ref.dropFirst("embedded:".count))" }
        return "未知语言"
    }

    /// 选哪条轨：优先上次记住的（"off" = 用户明确关掉，必须尊重），其次服务端裁决的默认轨；都没有就不自动开。
    func initialSelection(remembered: String?) -> String? {
        if remembered == "off" { return nil }
        if let remembered, options.contains(where: { $0.ref == remembered }) { return remembered }
        return options.first { $0.isDefault }?.ref
    }
}

/// 音轨菜单项（对应 Web `lib/player/audio-tracks.ts`）。只有一条轨时返回空——没得选的菜单是纯噪音。
struct AudioOption: Identifiable, Hashable {
    let ref: String
    let label: String
    let isDefault: Bool
    var id: String { ref }

    var embeddedIndex: Int? { ref.hasPrefix("embedded:") ? Int(ref.dropFirst("embedded:".count)) : nil }

    static func plan(_ tracks: [API.AudioTrackView]) -> [AudioOption] {
        guard tracks.count >= 2 else { return [] }
        return tracks.map { AudioOption(ref: $0.ref, label: label($0), isDefault: $0.isDefault) }
    }

    private static let channelLabels = [1: "单声道", 2: "立体声", 6: "5.1", 8: "7.1"]

    /// 语言 · 编码 · 声道（语言放最前：用户找的是「国语还是日语」）
    static func label(_ track: API.AudioTrackView) -> String {
        let name = LanguageLabel.of(track.language)
            ?? (track.ref.hasPrefix("embedded:") ? "音轨 \(track.ref.dropFirst("embedded:".count))" : "未知音轨")
        var rest: [String] = []
        if let codec = track.codec, !codec.isEmpty { rest.append(codec.uppercased()) }
        if let channels = track.channels, channels > 0 { rest.append(channelLabels[channels] ?? "\(channels) 声道") }
        return rest.isEmpty ? name : ([name] + rest).joined(separator: " · ")
    }
}
