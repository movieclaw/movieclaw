import SwiftUI

/// 一条字幕。时间是**文件时间**（秒）：服务端从整个文件抽出/转换的字幕，时间轴与原片一致。
struct SubtitleCue: Equatable {
    let start: Double
    let end: Double
    let text: String
}

/// WebVTT 解析（服务端已把 SRT/ASS 统一转成 VTT）。
///
/// 富文本标签一律剥掉（同 Web `plainCueText`）：字幕文件是用户丢进媒体库的任意文本，
/// 斜体这点观感不值得引入一个富文本解析器；要完整排版的 ASS 走 MPV 引擎（libass 原样渲染）。
/// cue 设置（line/position）也忽略——位置由播放器的「字幕位置」统一控制，与网页一致。
enum WebVTT {
    static func parse(_ raw: String) -> [SubtitleCue] {
        var cues: [SubtitleCue] = []
        let blocks = raw.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n\n")
        for block in blocks {
            let lines = block.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
            guard let timingIndex = lines.firstIndex(where: { $0.contains("-->") }) else { continue }
            let parts = lines[timingIndex].components(separatedBy: "-->")
            guard parts.count == 2,
                  let start = timestamp(parts[0]),
                  let end = timestamp(parts[1].trimmingCharacters(in: .whitespaces).components(separatedBy: " ").first ?? "")
            else { continue }
            let text = lines[(timingIndex + 1)...]
                .map { $0.replacingOccurrences(of: "<[^>]*>", with: "", options: .regularExpression) }
                .joined(separator: "\n")
                .replacingOccurrences(of: "&amp;", with: "&")
                .replacingOccurrences(of: "&lt;", with: "<")
                .replacingOccurrences(of: "&gt;", with: ">")
                .replacingOccurrences(of: "&nbsp;", with: " ")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty { cues.append(SubtitleCue(start: start, end: end, text: text)) }
        }
        return cues.sorted { $0.start < $1.start }
    }

    /// `hh:mm:ss.mmm` 或 `mm:ss.mmm`
    static func timestamp(_ raw: String) -> Double? {
        let trimmed = raw.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: ".")
        let parts = trimmed.split(separator: ":").map(String.init)
        guard (2 ... 3).contains(parts.count) else { return nil }
        var seconds = 0.0
        for part in parts {
            guard let value = Double(part) else { return nil }
            seconds = seconds * 60 + value
        }
        return seconds
    }

    /// 当前该显示的 cue（可能多条重叠）
    static func active(_ cues: [SubtitleCue], at time: Double) -> [SubtitleCue] {
        // cues 按开始时间排序；二分找到最后一个 start <= time 的位置再往前扫重叠的
        var low = 0, high = cues.count
        while low < high {
            let mid = (low + high) / 2
            if cues[mid].start <= time { low = mid + 1 } else { high = mid }
        }
        var result: [SubtitleCue] = []
        var index = low - 1
        while index >= 0, result.count < 3 {
            let cue = cues[index]
            if cue.end > time { result.insert(cue, at: 0) }
            if time - cue.start > 30 { break }
            index -= 1
        }
        return result
    }
}

/// AVPlayer 模式的文本字幕叠加层（对应 Web `subtitle-layer.tsx` 的 overlay 渲染器）。
///
/// 锚定**画面矩形**（按视频宽高比在播放区里 aspect-fit 出来的那块），字号是画面高度的百分比、
/// 位置是距画面底边的百分比——横竖屏切换、黑边多少都不影响字幕相对画面的样子。
/// 时间轴偏移：正数 = 字幕延后（cue 在 start + offset 时才出现）。
struct SubtitleOverlay: View {
    let url: URL?
    let style: SubtitleStyle
    let videoSize: CGSize
    /// 当前文件时间（秒）
    let time: () -> Double
    let session: URLSession

    @State private var cues: [SubtitleCue] = []

    var body: some View {
        GeometryReader { proxy in
            let rect = videoRect(in: proxy.size)
            TimelineView(.periodic(from: .now, by: 0.1)) { _ in
                let active = WebVTT.active(cues, at: time() - style.offsetSeconds)
                if !active.isEmpty {
                    let fontSize = max(12, rect.height * style.fontScale / 100)
                    Text(active.map(\.text).joined(separator: "\n"))
                        .font(.system(size: fontSize, weight: .medium))
                        .foregroundStyle(.white)
                        .multilineTextAlignment(.center)
                        .lineSpacing(fontSize * 0.1)
                        .modifier(SubtitleOutline(enabled: style.outline && !style.background))
                        .padding(.horizontal, style.background ? fontSize * 0.35 : 0)
                        .padding(.vertical, style.background ? fontSize * 0.12 : 0)
                        .background(style.background ? Color.black.opacity(0.6) : .clear, in: .rect(cornerRadius: fontSize * 0.2))
                        .frame(width: rect.width * 0.9)
                        .position(x: rect.midX, y: rect.maxY - rect.height * style.bottomPercent / 100 - fontSize)
                        .accessibilityIdentifier("player-subtitle")
                }
            }
        }
        .allowsHitTesting(false)
        .task(id: url) { await load() }
    }

    private func videoRect(in size: CGSize) -> CGRect {
        guard videoSize.width > 0, videoSize.height > 0 else { return CGRect(origin: .zero, size: size) }
        let scale = min(size.width / videoSize.width, size.height / videoSize.height)
        let fitted = CGSize(width: videoSize.width * scale, height: videoSize.height * scale)
        return CGRect(x: (size.width - fitted.width) / 2, y: (size.height - fitted.height) / 2, width: fitted.width, height: fitted.height)
    }

    private func load() async {
        cues = []
        guard let url else { return }
        // 内封轨首次要服务端通读整个容器抽出来（大文件可达数十秒），超时放宽到 5 分钟
        var request = URLRequest(url: url)
        request.timeoutInterval = 300
        guard let (data, response) = try? await session.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let text = String(data: data, encoding: .utf8) else { return }
        cues = WebVTT.parse(text)
    }
}

/// 描边：四向硬阴影近似描边（SwiftUI 没有文字描边原语）
private struct SubtitleOutline: ViewModifier {
    let enabled: Bool

    func body(content: Content) -> some View {
        if enabled {
            content
                .shadow(color: .black, radius: 0, x: 1, y: 1)
                .shadow(color: .black, radius: 0, x: -1, y: -1)
                .shadow(color: .black, radius: 0, x: 1, y: -1)
                .shadow(color: .black, radius: 0, x: -1, y: 1)
                .shadow(color: .black.opacity(0.6), radius: 3)
        } else {
            content
        }
    }
}
