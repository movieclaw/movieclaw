import SwiftUI

/// 条目详情页的「音轨 / 字幕」区块（对应 Web `components/media-track-rows.tsx` 的 MediaTrackRows，
/// 电影与剧集共用）。放在详情页的 ScrollView/VStack 里，不是整页。
///
/// 为什么不是一排徽章（设计沿用 Web）：
/// - 一个文件可能带十几条同语言字幕（内封 + 外挂 + AI 生成）。逐条铺徽章时，摘要行只是把
///   同一个答案（「有中文字幕」）重复十遍；真正有区分度的格式、来源、文件名反而看不到。
/// - 所以折叠态按**语言**去重成芯片（`简体中文 ×4`），手机上最多 2 枚、其余折成「+N 种」；
///   点芯片从底部弹出按语言分组的完整列表，并高亮、滚到被点的那一组。
/// - 芯片统一中性色；编码族的彩色只留在列表行首的格式色块上（在那里才有区分作用）。
///
/// 多文件（多版本 / 多分集）时先选物理文件再看轨道，避免不同版本的语言与格式混在一起；
/// 只认在位文件（`state == in_place`），缺失与待回收的版本不出现在选择器里。
///
/// 字幕行可点开预览（在列表弹层里推进一页，含一键校准时间轴）；外挂字幕（含 AI 生成）管理员可左滑删除；
/// 字幕行尾挂「AI 生成字幕」入口（仅管理员，见 `TrackSubtitleGenButton`）。
struct MediaTrackSection: View {
    /// 详情接口 `LibraryItemDetailView.files`（当前选中单元对应的文件集合，由调用方传入）
    let files: [API.LibraryFileView]
    @Binding var selectedFileId: Int?
    /// 字幕增删 / 校准 / 生成落盘后回调，调用方重拉详情
    var onChanged: () async -> Void = {}

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Feedback.self) private var feedback

    /// 展开中的轨道列表（音轨或字幕）
    @State private var listTarget: TrackListTarget?

    private var availableFiles: [API.LibraryFileView] { files.filter { $0.state == "in_place" } }

    /// 文件刷新或删除后，若原选择已不存在，直接派生回第一项
    private var selectedFile: API.LibraryFileView? {
        availableFiles.first { $0.id == selectedFileId } ?? availableFiles.first
    }

    var body: some View {
        if let file = selectedFile {
            content(file)
        }
    }

    private func content(_ file: API.LibraryFileView) -> some View {
        let audioGroups = TrackModel.groupByLanguage(TrackModel.audioEntries(file.audioStreams ?? []))
        let subtitleGroups = TrackModel.groupByLanguage(
            TrackModel.subtitleEntries(file.subtitleStreams, videoStem: TrackModel.videoStem(file.fileName))
        )
        let audioSpec = TrackModel.topAudioSpec(file.audioStreams ?? [])

        return VStack(alignment: .leading, spacing: 10) {
            if availableFiles.count > 1 {
                versionPicker(file)
            }

            TrackRowView(
                label: "音轨",
                groups: audioGroups,
                empty: file.audioStreams == nil ? "尚未探测" : "文件内没有音轨",
                onOpen: { listTarget = TrackListTarget(kind: .audio, focusLanguage: $0) }
            ) {
                if let audioSpec {
                    Text(audioSpec)
                        .font(.caption.weight(.semibold).monospacedDigit())
                        .foregroundStyle(Color.white.opacity(0.8))
                        .padding(.horizontal, 10)
                        .frame(height: 32)
                        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 7))
                        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.white.opacity(0.14)))
                }
            }

            TrackRowView(
                label: "字幕",
                groups: subtitleGroups,
                empty: "无内封或外挂字幕",
                onOpen: { listTarget = TrackListTarget(kind: .subtitle, focusLanguage: $0) }
            ) {
                // 生成入口紧跟在语言芯片后面：它是对「这一行的字幕」动手，挨着作用对象才读得通。
                // 按文件换身份：切版本时预检选择、任务跟踪一并重置（同 Web key={file.id}）
                TrackSubtitleGenButton(file: file, onChanged: onChanged)
                    .id(file.id)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .sheet(item: $listTarget) { target in
            // 列表内容取自当前（可能已重拉过的）文件：删除 / 校准一条字幕后列表就地刷新
            let currentFile = selectedFile ?? file
            let groups = target.kind == .audio
                ? TrackModel.groupByLanguage(TrackModel.audioEntries(currentFile.audioStreams ?? []))
                : TrackModel.groupByLanguage(TrackModel.subtitleEntries(
                    currentFile.subtitleStreams, videoStem: TrackModel.videoStem(currentFile.fileName)
                ))
            TrackListSheet(
                kind: target.kind,
                groups: groups,
                focusLanguage: target.focusLanguage,
                footer: target.kind == .subtitle
                    ? (permissions.canManageLibraries ? "点任意一条打开字幕预览；外挂字幕可左滑删除" : "点任意一条打开字幕预览")
                    : nil,
                canDelete: permissions.canManageLibraries,
                file: currentFile,
                onChanged: onChanged,
                onDelete: { entry in await delete(entry, in: currentFile) }
            )
            .sheetFeedback()
        }
    }

    /// 版本选择：「分辨率 — 文件名」
    private func versionPicker(_ file: API.LibraryFileView) -> some View {
        Menu {
            Picker("选择视频文件", selection: Binding(
                get: { file.id },
                set: { selectedFileId = $0 }
            )) {
                ForEach(availableFiles, id: \.id) { option in
                    Text(TrackModel.fileOptionLabel(option)).tag(option.id)
                }
            }
        } label: {
            HStack(spacing: 8) {
                Text(TrackModel.fileOptionLabel(file))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 8)
                Image(systemName: "chevron.up.chevron.down").font(.caption)
            }
            .font(.subheadline)
            .foregroundStyle(Color.white.opacity(0.9))
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity)
            .background(Color.white.opacity(0.055), in: .rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
        }
        .accessibilityLabel("选择视频文件")
    }

    /// 删除一条外挂字幕（确认已在列表弹层里做过：确认框列出完整路径，删除不进回收站也无法撤销）
    private func delete(_ entry: TrackEntry, in file: API.LibraryFileView) async {
        guard let filename = entry.deletable else { return }
        do {
            _ = try await api.librarySubtitlesDelete(fileId: file.id, filename: filename)
            feedback.success("已删除字幕：\(filename)")
            await onChanged()
        } catch is CancellationError {
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "字幕删除失败，请稍后重试" : error.localizedDescription)
        }
    }
}

// MARK: - 只读版（分享访客页）

/// 只读的音轨 / 字幕两行（对应 Web `ReadOnlyTrackRows`，分享访客页用）。
///
/// 语言分组与排序、AI 字幕的语言名、芯片上的格式标记、点开后按语言分组的完整列表，
/// 都与详情页的 `MediaTrackSection` 同一套（共用 `TrackModel` 与行/列表组件），
/// 只是没有版本选择、字幕预览、删除与 AI 生成——访客页不暴露任何管理入口。
/// 访客接口不下发文件名，外挂字幕的「同名前缀」无从推断，按空串处理（同 Web 传 ""）。
struct MediaTrackReadOnlyRows: View {
    let audioStreams: [API.AudioStreamView]?
    let subtitleStreams: [API.SubtitleStreamView]

    @State private var listTarget: TrackListTarget?

    var body: some View {
        let audioGroups = TrackModel.groupByLanguage(TrackModel.audioEntries(audioStreams ?? []))
        let subtitleGroups = TrackModel.groupByLanguage(TrackModel.subtitleEntries(subtitleStreams, videoStem: ""))
        let audioSpec = TrackModel.topAudioSpec(audioStreams ?? [])
        VStack(alignment: .leading, spacing: 10) {
            TrackRowView(
                label: "音轨",
                groups: audioGroups,
                empty: audioStreams == nil ? "尚未探测" : "文件内没有音轨",
                onOpen: { listTarget = TrackListTarget(kind: .audio, focusLanguage: $0) }
            ) {
                if let audioSpec {
                    Text(audioSpec)
                        .font(.caption.weight(.semibold).monospacedDigit())
                        .foregroundStyle(Color.white.opacity(0.8))
                        .padding(.horizontal, 10)
                        .frame(height: 32)
                        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 7))
                        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.white.opacity(0.14)))
                }
            }
            TrackRowView(
                label: "字幕",
                groups: subtitleGroups,
                empty: "无内封或外挂字幕",
                onOpen: { listTarget = TrackListTarget(kind: .subtitle, focusLanguage: $0) }
            ) {
                EmptyView()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .sheet(item: $listTarget) { target in
            TrackListSheet(
                kind: target.kind,
                groups: target.kind == .audio ? audioGroups : subtitleGroups,
                focusLanguage: target.focusLanguage,
                footer: nil,
                canDelete: false,
                file: nil,
                onDelete: { _ in }
            )
            .sheetFeedback()
        }
    }
}

// MARK: - 中性轨道模型：音轨与字幕拍平成同一种结构，分组 / 排序 / 渲染只写一份

private enum TrackKind {
    case audio, subtitle

    var title: String { self == .audio ? "音轨" : "字幕" }
}

private struct TrackListTarget: Identifiable {
    var kind: TrackKind
    var focusLanguage: String
    var id: String { "\(kind)-\(focusLanguage)" }
}

private struct TrackPreviewInfo {
    var stream: API.SubtitleStreamView
    var track: String
    var label: String
}

/// 格式色块的色调：只承担格式识别，刻意保持低饱和，避免重新变成徽章墙
private enum TrackTone {
    case srt, ass, pgs, vobsub, text, ai, other

    var background: Color {
        switch self {
        case .srt: Color(red: 0.49, green: 0.83, blue: 0.99).opacity(0.1)
        case .ass: Color(red: 0.77, green: 0.71, blue: 0.99).opacity(0.1)
        case .pgs: Color(red: 0.99, green: 0.83, blue: 0.30).opacity(0.1)
        case .vobsub: Color(red: 0.99, green: 0.73, blue: 0.45).opacity(0.1)
        case .text: Color(red: 0.40, green: 0.91, blue: 0.98).opacity(0.1)
        // AI 产物沿用主按钮的冷银色，与其他格式色块保持同一层级
        case .ai: Theme.accent.opacity(0.12)
        case .other: Color.white.opacity(0.065)
        }
    }

    var foreground: Color {
        switch self {
        case .srt: Color(red: 0.88, green: 0.95, blue: 1.0).opacity(0.9)
        case .ass: Color(red: 0.93, green: 0.91, blue: 1.0).opacity(0.9)
        case .pgs: Color(red: 1.0, green: 0.95, blue: 0.78).opacity(0.9)
        case .vobsub: Color(red: 1.0, green: 0.93, blue: 0.84).opacity(0.9)
        case .text: Color(red: 0.81, green: 0.98, blue: 1.0).opacity(0.9)
        case .ai: Theme.accentStrong.opacity(0.9)
        case .other: Color.white.opacity(0.75)
        }
    }
}

private struct TrackEntry: Identifiable {
    var id: String
    /// 分组键：面向用户的语言名（chi/zho/cmn 会并成同一个「中文」）
    var language: String
    /// 语言排序权重，越小越靠前
    var rank: Int
    /// 行首色块文案：SRT / ASS / PGS / TrueHD…
    var format: String
    var tone: TrackTone
    /// 主文案：音轨给规格，字幕给文件名或内封轨号
    var primary: String
    /// 次文案：音轨给轨号，字幕给来源（内封 / 外挂 / AI 外挂）
    var secondary: String
    /// 属性标记：默认 / 强制
    var flags: [String]
    /// 是否外挂文件；音轨没有内封/外挂之分，恒为 nil
    var external: Bool?
    /// 组内排序：默认 → 内封 → 外挂 → AI 生成
    var order: Int
    /// 可删除的外挂字幕文件名；内封轨与音轨为 nil（长在容器里，删不掉）
    var deletable: String?
    /// 仅字幕有：点击后打开预览所需的一切
    var preview: TrackPreviewInfo?
}

private struct TrackLanguageGroup: Identifiable {
    var language: String
    var entries: [TrackEntry]
    var rank: Int
    var hasDefault: Bool
    var id: String { language }
}

/// 展示格式化：ffprobe 原始值 → 用户认知的规格语言（逐项照搬 Web）
private enum TrackModel {
    /// 音轨编码的完整读法（列表行主文案）
    static let audioCodecLabels = [
        "aac": "AAC", "ac3": "Dolby Digital", "eac3": "Dolby Digital+", "truehd": "Dolby TrueHD",
        "dts": "DTS", "flac": "FLAC", "opus": "Opus", "mp3": "MP3", "vorbis": "Vorbis",
    ]
    /// 同一批编码的紧凑写法（行首格式色块）
    static let audioFormatTokens = [
        "aac": "AAC", "ac3": "AC3", "eac3": "EAC3", "truehd": "TrueHD", "dts": "DTS",
        "flac": "FLAC", "opus": "Opus", "mp3": "MP3", "vorbis": "Vorbis",
    ]
    static let subtitleCodecLabels = [
        "subrip": "SRT", "srt": "SRT", "ass": "ASS", "ssa": "SSA", "hdmv_pgs_subtitle": "PGS",
        "dvd_subtitle": "VobSub", "mov_text": "Text", "webvtt": "VTT", "vtt": "VTT", "sub": "VobSub", "sup": "PGS",
    ]
    /// 有序：按中文名反查语言码时取第一个命中（同 Web Object.keys 的插入顺序）
    static let languageLabels: [(code: String, label: String)] = [
        ("chs", "简体中文"), ("cht", "繁体中文"), ("chi", "中文"), ("zho", "中文"), ("cmn", "中文"),
        ("yue", "粤语"), ("eng", "英语"), ("jpn", "日语"), ("kor", "韩语"), ("fre", "法语"), ("fra", "法语"),
        ("ger", "德语"), ("deu", "德语"), ("spa", "西班牙语"), ("rus", "俄语"), ("ita", "意大利语"),
        ("por", "葡萄牙语"), ("tha", "泰语"), ("hin", "印地语"),
    ]
    /// 这是一款中文软件：中文优先；简繁刻意不合并——它们正是用户真正要区分的东西
    static let languageRanks = [
        "chs": 1, "cht": 2, "chi": 3, "zho": 3, "cmn": 3, "yue": 4, "eng": 5, "jpn": 6, "kor": 7,
    ]
    static let otherLanguageRank = 40
    static let bilingualRank = 80
    static let unknownLanguageRank = 90
    static let unknownLanguage = "未标语言"

    /// 这些 profile 只是编码内部档次，单独展示反而让人困惑；只有 DTS-HD MA 这类更有信息量的才顶替 codec
    static let genericProfiles: Set<String> = ["lc", "main", "high", "baseline", "main 10"]

    /// 音轨行右端常显的最高规格：评分先看编码档次再看声道数
    static let audioCodecTier = [
        "truehd": 5, "dts": 4, "eac3": 3, "flac": 3, "ac3": 2, "opus": 1, "aac": 1, "vorbis": 0, "mp3": 0,
    ]

    static func languageLabel(_ code: String?) -> String? {
        guard let code, code != "und" else { return nil }
        let lower = code.lowercased()
        return languageLabels.first { $0.code == lower }?.label ?? code
    }

    static func languageRank(_ code: String?) -> Int {
        guard let code, code != "und" else { return unknownLanguageRank }
        return languageRanks[code.lowercased()] ?? otherLanguageRank
    }

    /// 声道数 → 惯用布局标签（channel_layout 可用时优先，去掉 (side) 等后缀）
    static func channelsLabel(_ stream: API.AudioStreamView) -> String? {
        if let layout = stream.channelLayout?.split(separator: "(", omittingEmptySubsequences: false).first?
            .trimmingCharacters(in: .whitespaces),
            let first = layout.first, first.isASCII, first.isNumber {
            return layout
        }
        guard let channels = stream.channels else { return nil }
        let map = [1: "单声道", 2: "2.0", 6: "5.1", 7: "6.1", 8: "7.1"]
        return map[channels] ?? "\(channels) 声道"
    }

    static func informativeProfile(_ stream: API.AudioStreamView) -> String? {
        guard let profile = stream.profile, !profile.isEmpty else { return nil }
        return genericProfiles.contains(profile.lowercased()) ? nil : profile
    }

    static func audioCodecName(_ stream: API.AudioStreamView) -> String? {
        guard let codec = stream.codec, !codec.isEmpty else { return nil }
        return audioCodecLabels[codec.lowercased()] ?? codec.uppercased()
    }

    /// 音轨 → 列表行主文案：格式（有信息量的 profile 优先）· 声道
    static func audioSpecLabel(_ stream: API.AudioStreamView) -> String {
        let codec = informativeProfile(stream) ?? audioCodecName(stream) ?? "未知格式"
        return [codec, channelsLabel(stream)].compactMap { $0 }.joined(separator: " · ")
    }

    /// 音轨没有可点动作，摘要行要自己回答「这片音质到什么档次」；只有一条音轨时芯片已写格式，不重复挂
    static func topAudioSpec(_ streams: [API.AudioStreamView]) -> String? {
        guard streams.count >= 2 else { return nil }
        var best: API.AudioStreamView?
        var bestScore = -1
        for stream in streams {
            let tier = audioCodecTier[stream.codec?.lowercased() ?? ""] ?? 0
            let score = tier * 100 + (stream.channels ?? 0)
            if score > bestScore {
                bestScore = score
                best = stream
            }
        }
        guard let best else { return nil }
        let codec = informativeProfile(best) ?? audioCodecName(best)
        let text = [codec, channelsLabel(best)].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " ")
        return text.isEmpty ? nil : text
    }

    /// AI 生成字幕的标题里编了目标语言，翻译回中文读法（双语保留两个语言）
    static func generatedSubtitleLabel(_ stream: API.SubtitleStreamView) -> String? {
        guard let title = stream.title?.lowercased(), !title.isEmpty else { return nil }
        if title.hasPrefix("ai-bilingual-") {
            let languages = title.dropFirst("ai-bilingual-".count).split(separator: "-", omittingEmptySubsequences: false).map(String.init)
            if languages.count == 2 {
                return "\(languageLabel(languages[0]) ?? languages[0]) + \(languageLabel(languages[1]) ?? languages[1])"
            }
        }
        if title == "ai-chs" { return "简体中文" }
        if title == "ai-cht" { return "繁体中文" }
        return nil
    }

    /// 字幕 → 分组用的语言名（AI 产物的语言写在标题里，优先认它）
    static func subtitleLanguageName(_ stream: API.SubtitleStreamView) -> String {
        generatedSubtitleLabel(stream)
            ?? languageLabel(stream.language)
            ?? stream.title?.trimmingCharacters(in: .whitespacesAndNewlines)
            ?? unknownLanguage
    }

    static func subtitleFormatToken(_ stream: API.SubtitleStreamView) -> String {
        guard let codec = stream.codec, !codec.isEmpty else { return "字幕" }
        return subtitleCodecLabels[codec.lowercased()] ?? codec.uppercased()
    }

    static func fileTokens(_ stream: API.SubtitleStreamView) -> [String] {
        stream.fileName?.lowercased().split(separator: ".", omittingEmptySubsequences: false).map(String.init) ?? []
    }

    static func isAiSubtitle(_ stream: API.SubtitleStreamView) -> Bool {
        let title = stream.title?.lowercased() ?? ""
        return title.hasPrefix("ai-") || fileTokens(stream).contains { $0 == "ai" || $0.hasPrefix("ai-") }
    }

    static func subtitleTone(_ stream: API.SubtitleStreamView) -> TrackTone {
        if isAiSubtitle(stream) { return .ai }
        switch stream.codec?.lowercased() {
        case "subrip", "srt": return .srt
        case "ass", "ssa": return .ass
        case "hdmv_pgs_subtitle", "sup": return .pgs
        case "dvd_subtitle", "sub": return .vobsub
        case "mov_text", "webvtt", "vtt": return .text
        default: return .other
        }
    }

    /// 音轨与字幕共用低饱和色系，颜色只帮助快速区分常见编码族
    static func audioTone(_ stream: API.AudioStreamView) -> TrackTone {
        switch stream.codec?.lowercased() {
        case "aac", "mp3": .srt
        case "truehd": .ass
        case "ac3", "eac3": .pgs
        case "dts": .vobsub
        case "flac", "opus", "vorbis": .text
        default: .other
        }
    }

    static func externalSubtitleSuffix(_ stream: API.SubtitleStreamView) -> String {
        let tokens = fileTokens(stream)
        if tokens.contains(where: { $0 == "ai" || $0.hasPrefix("ai-") }) { return "AI 外挂" }
        if tokens.contains("pgs-ocr") { return "PGS 转换" }
        return "外挂"
    }

    /// 外挂字幕的行内标签：去掉与视频同名的那段前缀，只留区分用的 token 段。
    /// 外挂字幕全都以视频文件名开头，完整文件名截断后几条长得一模一样；完整路径仍在删除确认框里给全。
    static func externalSubtitleLabel(_ fileName: String?, videoStem: String) -> String {
        guard let fileName else { return "外挂字幕" }
        if !videoStem.isEmpty, fileName.lowercased().hasPrefix(videoStem.lowercased()) {
            var rest = String(fileName.dropFirst(videoStem.count))
            if rest.hasPrefix(".") { rest.removeFirst() }
            if !rest.isEmpty { return rest }
        }
        return fileName
    }

    /// 视频文件名去掉扩展名
    static func videoStem(_ fileName: String) -> String {
        guard let dot = fileName.lastIndex(of: "."), fileName.index(after: dot) < fileName.endIndex else { return fileName }
        return String(fileName[..<dot])
    }

    /// 外挂字幕与视频同目录，据此还原它的完整路径（删除确认框要摆给用户看）
    static func siblingPath(_ videoPath: String, _ filename: String) -> String {
        guard let cut = videoPath.lastIndex(where: { $0 == "/" || $0 == "\\" }) else { return filename }
        return String(videoPath[...cut]) + filename
    }

    /// 字幕预览接口使用的中性轨引用：内封按序号，外挂按文件名
    static func subtitleTrackRef(_ stream: API.SubtitleStreamView, index: Int) -> String? {
        if !stream.external { return "embedded:\(index)" }
        return stream.fileName.map { "external:\($0)" }
    }

    static func fileOptionLabel(_ file: API.LibraryFileView) -> String {
        guard let resolution = file.resolution, !resolution.isEmpty else { return file.fileName }
        return "\(formatVideoResolution(resolution)) — \(file.fileName)"
    }

    /// 同 Web `formatVideoResolution`
    static func formatVideoResolution(_ resolution: String) -> String {
        let normalized = resolution.trimmingCharacters(in: .whitespaces).lowercased()
        let key = normalized.allSatisfy(\.isNumber) && !normalized.isEmpty ? "\(normalized)p" : normalized
        let labels = ["4320p": "8K", "2160p": "4K", "1440p": "2K", "1080p": "1080p", "720p": "720p", "4k": "4K", "2k": "2K"]
        return labels[key] ?? resolution
    }

    static func audioEntries(_ streams: [API.AudioStreamView]) -> [TrackEntry] {
        streams.enumerated().map { index, stream in
            let spec = audioSpecLabel(stream)
            let title = stream.title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let format: String = {
                guard let codec = stream.codec, !codec.isEmpty else { return "音轨" }
                return audioFormatTokens[codec.lowercased()] ?? codec.uppercased()
            }()
            return TrackEntry(
                id: "audio:\(index)",
                language: languageLabel(stream.language) ?? unknownLanguage,
                rank: languageRank(stream.language),
                format: format,
                tone: audioTone(stream),
                primary: title.isEmpty ? spec : "\(spec) · \(title)",
                secondary: "轨 \(index + 1)",
                flags: stream.default ? ["默认"] : [],
                external: nil,
                order: stream.default ? 0 : 1,
                deletable: nil,
                preview: nil
            )
        }
    }

    static func subtitleEntries(_ streams: [API.SubtitleStreamView], videoStem: String) -> [TrackEntry] {
        // 内封轨在界面上按「内封里的第几条」编号，而不是混合数组的下标——
        // subtitle_streams 里内封与外挂混在一起，直接用下标会给出错误的轨号
        var embeddedOrdinal = 0
        return streams.enumerated().map { index, stream in
            let language = subtitleLanguageName(stream)
            let format = subtitleFormatToken(stream)
            let external = stream.external
            if !external { embeddedOrdinal += 1 }
            let track = subtitleTrackRef(stream, index: index)
            let label = "\(language) · \(format)"
            var flags: [String] = []
            if stream.default { flags.append("默认") }
            if stream.forced { flags.append("强制") }
            return TrackEntry(
                id: "subtitle:\(track ?? "unknown:\(index)")",
                language: language,
                rank: subtitleRank(stream, language: language),
                format: format,
                tone: subtitleTone(stream),
                primary: external ? externalSubtitleLabel(stream.fileName, videoStem: videoStem) : "内封轨 \(embeddedOrdinal)",
                secondary: external ? externalSubtitleSuffix(stream) : "内封",
                flags: flags,
                external: external,
                order: stream.default ? 0 : isAiSubtitle(stream) ? 3 : external ? 2 : 1,
                // 外挂（含 AI 生成）是磁盘上的独立文件，可以单独删；内封轨要删得重封装视频本体，不给入口
                deletable: external ? stream.fileName : nil,
                preview: track.map { TrackPreviewInfo(stream: stream, track: $0, label: label) }
            )
        }
    }

    static func subtitleRank(_ stream: API.SubtitleStreamView, language: String) -> Int {
        if language.contains(" + ") { return bilingualRank }
        if language == unknownLanguage { return unknownLanguageRank }
        let byCode = languageRank(stream.language)
        if byCode != unknownLanguageRank { return byCode }
        // AI 产物的 language 字段常常是空的，语言只写在标题里，按中文名反查权重
        if let code = languageLabels.first(where: { $0.label == language })?.code { return languageRank(code) }
        return otherLanguageRank
    }

    /// 按语言分组并排序：默认轨所在语言 → 简中 → 繁中 → 中文/粤语 → 其余按条数降序 →
    /// 双语与未标语言垫底。组内：默认 → 内封 → 外挂 → AI 生成。
    static func groupByLanguage(_ entries: [TrackEntry]) -> [TrackLanguageGroup] {
        var order: [String] = []
        var buckets: [String: [TrackEntry]] = [:]
        for entry in entries {
            if buckets[entry.language] == nil { order.append(entry.language) }
            buckets[entry.language, default: []].append(entry)
        }
        let groups = order.map { language in
            let list = buckets[language] ?? []
            return TrackLanguageGroup(
                language: language,
                entries: list.sorted { $0.order < $1.order },
                rank: list.map(\.rank).min() ?? otherLanguageRank,
                hasDefault: list.contains { $0.order == 0 }
            )
        }
        let chinese = Locale(identifier: "zh_Hans_CN")
        return groups.sorted { a, b in
            if a.hasDefault != b.hasDefault { return a.hasDefault }
            if a.rank != b.rank { return a.rank < b.rank }
            if a.entries.count != b.entries.count { return a.entries.count > b.entries.count }
            return a.language.compare(b.language, locale: chinese) == .orderedAscending
        }
    }
}

// MARK: - 折叠态：一行轨道摘要

/// 左侧行名，中间语言芯片（手机最多 2 枚，其余折成「+N 种」），末尾挂行级尾件
/// （音轨给最高规格，字幕给 AI 生成入口）。点芯片从底部弹出完整列表。
private struct TrackRowView<Trailing: View>: View {
    let label: String
    let groups: [TrackLanguageGroup]
    let empty: String
    let onOpen: (String) -> Void
    @ViewBuilder let trailing: () -> Trailing

    /// 手机一行放不下三枚还带尾件（同 Web 移动端）
    private let maxChips = 2

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 16) {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(Theme.textFaint)
                .frame(width: 40, alignment: .leading)
            TrackFlowLayout(spacing: 6, lineSpacing: 6) {
                if groups.isEmpty {
                    Text(empty)
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                        .frame(height: 32)
                } else {
                    ForEach(groups.prefix(maxChips)) { group in
                        Button { onOpen(group.language) } label: {
                            HStack(spacing: 6) {
                                Text(group.language)
                                if group.entries.count == 1 {
                                    Text(group.entries[0].format)
                                        .font(.caption2.weight(.semibold))
                                        .foregroundStyle(Color.white.opacity(0.5))
                                } else {
                                    Text("×\(group.entries.count)")
                                        .fontWeight(.semibold)
                                        .foregroundStyle(Color.white.opacity(0.5))
                                }
                            }
                            .modifier(TrackChipStyle(background: Color.white.opacity(0.075), foreground: Color.white.opacity(0.85)))
                        }
                        .buttonStyle(.plain)
                    }
                    let hidden = groups.dropFirst(maxChips)
                    if let first = hidden.first {
                        Button { onOpen(first.language) } label: {
                            Text("+\(hidden.count) 种")
                                .modifier(TrackChipStyle(background: Color.white.opacity(0.045), foreground: Theme.textMuted))
                        }
                        .buttonStyle(.plain)
                    }
                }
                trailing()
            }
        }
    }
}

private struct TrackChipStyle: ViewModifier {
    let background: Color
    let foreground: Color

    func body(content: Content) -> some View {
        content
            .font(.caption.weight(.medium).monospacedDigit())
            .foregroundStyle(foreground)
            .padding(.horizontal, 10)
            .frame(height: 32)
            .background(background, in: .rect(cornerRadius: 7))
            .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.white.opacity(0.06)))
    }
}

// MARK: - 展开态：按语言分组的完整列表（底部弹层）

/// 按语言分组的完整轨道列表。与订阅类弹层同一套 iOS 26 原生形态（见 `SubsSheetScaffold` 的说明）：
/// - 不自设弹层背景，停在贴合高度时是系统悬浮的液态玻璃；左上 ✕ 关闭；
/// - 正文是原生分组列表，一种语言一个 Section，「共几条 · 内封/外挂各几条」放导航栏副标题；
/// - 点字幕在**同一个弹层里推进**到预览页（左上角返回列表），而不是先收起列表再弹第二个弹层；
///   推进时弹层自动拉到全高——字幕对白动辄上千条，贴合高度下只露几行；
/// - 删除外挂字幕用系统的左滑删除，确认框列出完整路径（删除不进回收站、无法撤销）。
private struct TrackListSheet: View {
    let kind: TrackKind
    let groups: [TrackLanguageGroup]
    let focusLanguage: String
    let footer: String?
    let canDelete: Bool
    /// 轨道所属的视频文件；nil = 访客页的只读列表（不给预览，外挂字幕的完整路径也无从还原）
    let file: API.LibraryFileView?
    /// 预览页里校准时间轴成功后回调，调用方重拉详情
    var onChanged: () async -> Void = {}
    let onDelete: (TrackEntry) async -> Void

    @Environment(\.dismiss) private var dismiss
    /// 待确认删除的一条；确认框就挂在列表弹层上（全局确认框在根视图，会被弹层挡住）
    @State private var pendingDelete: TrackEntry?
    /// 推进中的字幕预览（条目 id）
    @State private var path: [String] = []

    private var entries: [TrackEntry] { groups.flatMap(\.entries) }

    /// 导航栏副标题：总条数，字幕再拆内封 / 外挂（音轨没有这个区分，不占位）
    private var summary: String {
        let total = entries.count
        guard entries.contains(where: { $0.external != nil }) else { return "共 \(total) 条" }
        let external = entries.filter { $0.external == true }.count
        return "共 \(total) 条 · 内封 \(total - external) · 外挂 \(external)"
    }

    var body: some View {
        NavigationStack(path: $path) {
            ScrollViewReader { proxy in
                Form {
                    ForEach(groups) { group in
                        Section {
                            ForEach(group.entries) { entry in
                                row(entry)
                            }
                        } header: {
                            HStack(spacing: 6) {
                                Text(group.language)
                                Text("· \(group.entries.count)").monospacedDigit()
                            }
                            // 被点的那一组标题用强调色：滚过去之后依然认得出
                            .foregroundStyle(group.language == focusLanguage ? Theme.accentStrong : Theme.textMuted)
                        } footer: {
                            if let footer, group.id == groups.last?.id {
                                Text(footer)
                            }
                        }
                        .id(group.language)
                    }
                }
                .subsFormStyle()
                // 量的是这张列表的内容高度，所以挂在 Form 上而不是外层
                .modifier(SubsFittedDetents(fullHeight: !path.isEmpty))
                // 被点的语言组滚进视野（内容超过弹层高度时才有意义）。点的是第一组就不滚：
                // 列表本来就从它开始，硬滚会把组标题顶进导航栏底下
                .onAppear {
                    if focusLanguage != groups.first?.language { proxy.scrollTo(focusLanguage, anchor: .top) }
                }
            }
            .navigationTitle(kind.title)
            .navigationSubtitle(summary)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭", systemImage: "xmark", role: .close) { dismiss() }
                        .accessibilityIdentifier("sheet-close")
                }
            }
            .navigationDestination(for: String.self) { id in
                if let file, let entry = entries.first(where: { $0.id == id }), let preview = entry.preview {
                    TrackSubtitlePreviewPage(
                        file: file,
                        stream: preview.stream,
                        track: preview.track,
                        label: preview.label,
                        onChanged: onChanged
                    )
                }
            }
            .alert(
                "删除这个字幕文件？",
                isPresented: Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }),
                presenting: pendingDelete
            ) { entry in
                Button("取消", role: .cancel) { pendingDelete = nil }
                Button("删除字幕", role: .destructive) {
                    pendingDelete = nil
                    // 删除不收起列表：删完还能接着清理同一组里的其他字幕
                    Task { await onDelete(entry) }
                }
            } message: { entry in
                // 列真实路径而不是「这条字幕」：同一部影片常有一堆同语言字幕，只有路径能确认删的是哪个
                Text("将从磁盘直接删除下面的文件，删除后无法恢复：\n\(TrackModel.siblingPath(file?.filePath ?? "", entry.deletable ?? ""))")
            }
        }
    }

    /// 列表里的一条轨。字幕推进到预览页（行尾是系统的 ›），音轨只是一条信息；
    /// 可删的外挂字幕（含 AI 生成）挂系统左滑删除。
    @ViewBuilder
    private func row(_ entry: TrackEntry) -> some View {
        let selectable = file != nil && entry.preview != nil && kind == .subtitle
        Group {
            if selectable {
                NavigationLink(value: entry.id) { lineContent(entry) }
                    .accessibilityLabel("预览字幕：\(entry.language) · \(entry.format) · \(entry.primary)")
            } else {
                lineContent(entry)
            }
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            if canDelete, entry.deletable != nil {
                // 不用 role: .destructive：那会让系统先把行删掉动画走，而这里还要等确认框
                Button("删除", systemImage: "trash") { pendingDelete = entry }
                    .tint(.red)
                    .accessibilityLabel("删除字幕文件：\(entry.primary)")
            }
        }
    }

    private func lineContent(_ entry: TrackEntry) -> some View {
        HStack(spacing: 10) {
            Text(entry.format)
                .font(.caption2.weight(.bold))
                .foregroundStyle(entry.tone.foreground)
                .padding(.horizontal, 6)
                .frame(height: 20)
                .background(entry.tone.background, in: .rect(cornerRadius: 5))
            Text(entry.primary)
                .font(.subheadline)
                .foregroundStyle(Theme.text)
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 6) {
                Text(entry.secondary)
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
                ForEach(entry.flags, id: \.self) { flag in
                    Text(flag)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Theme.textMuted)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Color.white.opacity(0.07), in: .rect(cornerRadius: 5))
                }
            }
            .fixedSize()
        }
    }
}

// MARK: - 布局

/// 自动换行的横排（语言芯片行、生成进度的计数行共用）。
/// 子视图比整行还宽时（如「去接入」引导文案）按行宽重新求尺寸，让它自己折行。
struct TrackFlowLayout: Layout {
    var spacing: CGFloat
    var lineSpacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(width: proposal.width ?? .infinity, subviews: subviews)
        let height = rows.reduce(0) { $0 + $1.height } + lineSpacing * CGFloat(max(0, rows.count - 1))
        let width = rows.map(\.width).max() ?? 0
        return CGSize(width: proposal.width ?? width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in arrange(width: bounds.width, subviews: subviews) {
            var x = bounds.minX
            for item in row.items {
                subviews[item.index].place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(item.size))
                x += item.size.width + spacing
            }
            y += row.height + lineSpacing
        }
    }

    private struct Row {
        var items: [(index: Int, size: CGSize)] = []
        var width: CGFloat = 0
        var height: CGFloat = 0
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> [Row] {
        var rows: [Row] = []
        var row = Row()
        for index in subviews.indices {
            var size = subviews[index].sizeThatFits(.unspecified)
            if size.width > width { size = subviews[index].sizeThatFits(ProposedViewSize(width: width, height: nil)) }
            if !row.items.isEmpty, row.width + spacing + size.width > width {
                rows.append(row)
                row = Row()
            }
            row.width += (row.items.isEmpty ? 0 : spacing) + size.width
            row.height = max(row.height, size.height)
            row.items.append((index, size))
        }
        if !row.items.isEmpty { rows.append(row) }
        return rows
    }
}
