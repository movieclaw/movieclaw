import SwiftUI

/// 条目详情页的「章节」横排（对应 Web `components/chapter-strip.tsx`，设计见
/// docs/design/video-chapters.md §1.1）：每个章节一张 16:9 章节图 + 左下角时间戳 + 标题。
///
/// - 用户可见文案统一叫「章节」（用户决策 2026-09-06），合成章节与内嵌章节不区分；
/// - **看图优先，播放键不常驻**：触屏没有 hover，点卡片进灯箱看大图，灯箱顶栏有
///   「从此处播放」；小卡片上什么都不盖；
/// - 章节可能还没有图（后台正在抓 / 库关了开关 / ffmpeg 缺失）：卡片是深色占位 + 大号时间戳，
///   点它直接从该章起播——章节列表本身就有用，图是附属物；
/// - 起播时间用图上那一帧的真实时间（`frame_ms`），无图时退回章节起点；
/// - 灯箱与图床浏览同一个内核（缩放 / 滑动翻页 / 点画面收放控件），只放有图的章节。
///
/// 章节模型是 `API.ChapterView`（`LibraryFileView.chapters`，按文件给出）。
struct ChapterStripSection: View {
    let chapters: [API.ChapterView]
    /// 「从此处播放」，参数为秒
    var onPlayFrom: (Double) -> Void
    /// 章节图正在后台生成（`LibraryItemDetailView.chaptersPending`）：标题行右侧给个提示
    var pending: Bool = false
    /// 当前观看者上次看到的位置（毫秒）；落在哪一章就在那张卡标「上次看到这里」，并横滚到它
    var resumeMs: Int? = nil

    @Environment(\.api) private var api
    @State private var lightbox: ChapterLightboxSession?
    @State private var resumeScrolled = false
    /// 灯箱里点了「从此处播放」：等全屏层收起后再交给调用方（否则播放器弹不出来）
    @State private var pendingPlay: Double?

    var body: some View {
        if !chapters.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                header.padding(.horizontal, Theme.pagePadding)
                strip
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("章节")
            .fullScreenCover(item: $lightbox, onDismiss: {
                if let seconds = pendingPlay { onPlayFrom(seconds) }
                pendingPlay = nil
            }) { session in
                ChapterLightbox(slides: slides, chapters: withImages, index: session.index) { seconds in
                    pendingPlay = seconds
                }
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text("章节")
                .font(.title3.weight(.semibold))
                .foregroundStyle(Theme.text)
            Text("\(chapters.count) 个章节")
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(Theme.textFaint)
            Spacer(minLength: 0)
            if pending {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini)
                    Text("正在生成章节")
                }
                .font(.caption)
                .foregroundStyle(Theme.textMuted)
            }
        }
    }

    private var strip: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 12) {
                    ForEach(chapters, id: \.index) { chapter in
                        ChapterCard(
                            chapter: chapter,
                            imageURL: api.image(chapter.imageUrl, .landscapeCard),
                            resumeHere: chapter.index == resumeIndex
                        ) {
                            if chapter.imageUrl != nil, let i = withImages.firstIndex(where: { $0.index == chapter.index }) {
                                lightbox = ChapterLightboxSession(index: i)
                            } else {
                                onPlayFrom(Self.seconds(of: chapter))
                            }
                        }
                        .id(chapter.index)
                    }
                }
                .padding(.vertical, 2)
                .padding(.horizontal, Theme.pagePadding)
            }
            .scrollClipDisabled()
            // 首次知道续播章节时把它横滚到中间；之后用户自己滑不再抢位置
            .onChange(of: resumeIndex, initial: true) { _, index in
                guard !resumeScrolled, let index else { return }
                resumeScrolled = true
                Task { @MainActor in
                    withAnimation { proxy.scrollTo(index, anchor: .center) }
                }
            }
        }
    }

    /// 上次看到的位置落在哪一章：start ≤ pos < end（末章无 end 视为到片尾）
    private var resumeIndex: Int? {
        guard let resumeMs, resumeMs > 0 else { return nil }
        return chapters.first { chapter in
            resumeMs >= chapter.startMs && (chapter.endMs.map { resumeMs < $0 } ?? true)
        }?.index
    }

    /// 灯箱只放有图的章节
    private var withImages: [API.ChapterView] {
        chapters.filter { $0.imageUrl != nil }
    }

    private var slides: [LibraryZoomableImage.Slide] {
        withImages.map { chapter in
            LibraryZoomableImage.Slide(
                id: chapter.index,
                title: Self.caption(chapter),
                thumbURL: api.image(chapter.imageUrl, .landscapeCard),
                screenURL: api.image(chapter.imageUrl),
                aspect: 16.0 / 9.0
            )
        }
    }

    /// 起播秒数：图上那一帧的真实时间，无图时退回章节起点
    fileprivate static func seconds(of chapter: API.ChapterView) -> Double {
        Double(chapter.frameMs ?? chapter.startMs) / 1000
    }

    fileprivate static func clock(_ chapter: API.ChapterView) -> String {
        Formatters.clock(seconds(of: chapter))
    }

    /// 灯箱顶栏的章节说明：「标题 · 12:30」，无标题只给时间
    private static func caption(_ chapter: API.ChapterView) -> String {
        if let title = chapter.title { return "\(title) · \(clock(chapter))" }
        return clock(chapter)
    }
}

private struct ChapterLightboxSession: Identifiable {
    let id = UUID()
    var index: Int
}

/// 一张章节卡：16:9 画面 + 左下角时间戳（压暗底，任何画面上都读得清）+ 下方标题
private struct ChapterCard: View {
    let chapter: API.ChapterView
    let imageURL: URL?
    let resumeHere: Bool
    let onOpen: () -> Void

    var body: some View {
        let clock = ChapterStripSection.clock(chapter)
        let label = chapter.title ?? "第 \(chapter.index + 1) 章"
        VStack(alignment: .leading, spacing: 8) {
            Button(action: onOpen) {
                ZStack {
                    if imageURL != nil {
                        RemoteImage(url: imageURL, placeholderSymbol: "film")
                    } else {
                        // 还没有章节图：深色占位 + 大号时间戳
                        Theme.surfaceRaised
                        Text(clock)
                            .font(.system(size: 20, weight: .bold).monospacedDigit())
                            .foregroundStyle(.white.opacity(0.2))
                    }
                }
                .frame(width: 200, height: 200 * 9 / 16)
                .clipped()
                .overlay(alignment: .bottomLeading) {
                    Text(clock)
                        .font(.caption2.weight(.semibold).monospacedDigit())
                        .foregroundStyle(.white.opacity(0.9))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(.black.opacity(0.6), in: .rect(cornerRadius: 4))
                        .padding(6)
                }
                .overlay(alignment: .topTrailing) {
                    if resumeHere {
                        Text("上次看到这里")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 1)
                            // Web --accent-2（银蓝暗侧）
                            .background(Color(red: 0x9F / 255, green: 0xB0 / 255, blue: 0xC9 / 255), in: .rect(cornerRadius: 4))
                            .shadow(radius: 4)
                            .padding(6)
                    }
                }
                .clipShape(.rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.white.opacity(0.08)))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(chapter.imageUrl != nil ? "查看章节图：\(label) \(clock)" : "从 \(clock) 播放")

            Text(label)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(Theme.text)
                .lineLimit(1)
        }
        .frame(width: 200)
    }
}

/// 章节图灯箱：一次拿全，没有下一页；顶栏只有「从此处播放」
private struct ChapterLightbox: View {
    let slides: [LibraryZoomableImage.Slide]
    let chapters: [API.ChapterView]
    @State var index: Int
    let onPlay: (Double) -> Void

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        LibraryZoomableImage.Lightbox(slides: slides, index: $index) {
            LibraryZoomableImage.ActionButton(systemImage: "play.fill", label: "从此处播放") {
                guard chapters.indices.contains(index) else { return }
                onPlay(ChapterStripSection.seconds(of: chapters[index]))
                dismiss()
            }
        }
    }
}
