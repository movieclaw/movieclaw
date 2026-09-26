import NukeUI
import SwiftUI

/// 全屏看图灯箱（对应 Web `ImageLightbox` / `ZoomLightbox`）：左右滑动翻页、双指/双击缩放、
/// 顶部标题与序号，右上可挂调用方的操作键（种子图集的 详情 / 投给订阅 / 下载），底部是缩略图条（点击直达）。
/// 网页剧照灯箱底部的「设为背景」App 不做（App 没有背景图设定）。
///
/// 三级地址（同 Web 媒体库/种子灯箱）：缩略条用小图，舞台用屏幕档，放大后才换原图（`originals`）。
///
/// 用法：`.fullScreenCover(item: $lightbox) { DiscoverLightbox(content: $0).sheetFeedback() }`
struct DiscoverLightboxContent: Identifiable {
    let id = UUID()
    /// 舞台图地址（已解析好，如 `api.image(url, .photoScreen)` 或原图）
    var urls: [URL?]
    var initialIndex: Int = 0
    var title: String
    /// 图片加载失败时的补充说明（种子图床常失效）
    var brokenHint: String?
    /// 底部缩略图条的地址（nil = 不显示缩略条；只有一张图时也不显示）
    var thumbnails: [URL?]?
    /// 缩略图宽高比：竖版 2:3（海报/截图，默认）或宽幅 16:9（剧照）
    var thumbAspect: CGFloat = 2.0 / 3.0
    /// 放大后换用的原图地址（nil = 舞台图就是最高档）
    var originals: [URL?]?
    /// 顶栏右侧的操作键（调用方自带状态与环境）
    var accessory: AnyView?
}

struct DiscoverLightbox: View {
    let content: DiscoverLightboxContent
    @Environment(\.dismiss) private var dismiss
    @State private var index = 0
    /// 缩略条可视宽度：图少时居中排布
    @State private var stripWidth: CGFloat = 0

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            TabView(selection: $index) {
                ForEach(content.urls.indices, id: \.self) { i in
                    ZoomableImage(url: content.urls[i], originalURL: content.originals?[safe: i] ?? nil, brokenHint: content.brokenHint)
                        .tag(i)
                }
            }
            .tabViewStyle(.page(indexDisplayMode: .never))
            .ignoresSafeArea()
        }
        .overlay(alignment: .top) {
            HStack(spacing: 12) {
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark").font(.body.weight(.semibold)).frame(width: 36, height: 36)
                }
                .buttonStyle(.glass)
                .accessibilityLabel("关闭")
                .accessibilityIdentifier("lightbox-close")
                VStack(alignment: .leading, spacing: 2) {
                    Text(content.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                    Text("\(index + 1) / \(content.urls.count)").font(.caption).monospacedDigit().foregroundStyle(.white.opacity(0.7))
                }
                Spacer()
                if let accessory = content.accessory {
                    accessory
                }
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .padding(.top, 8)
        }
        .overlay(alignment: .bottom) {
            if let thumbnails = content.thumbnails, thumbnails.count > 1 {
                thumbnailStrip(thumbnails)
                    .padding(.bottom, 12)
            }
        }
        .onAppear { index = min(max(content.initialIndex, 0), max(content.urls.count - 1, 0)) }
        .preferredColorScheme(.dark)
        .statusBarHidden()
    }

    /// 底部缩略图条：当前项高亮环，点击直达（同 Web ImageLightbox：高 56，剧照宽 100、竖图宽 40）
    private func thumbnailStrip(_ thumbnails: [URL?]) -> some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(thumbnails.indices, id: \.self) { i in
                        Button {
                            withAnimation { index = i }
                        } label: {
                            RemoteImage(url: thumbnails[i], placeholderSymbol: "photo")
                                .frame(width: (56 * content.thumbAspect).rounded(), height: 56)
                                .background(Color.white.opacity(0.05))
                                .clipShape(.rect(cornerRadius: 6))
                                .overlay {
                                    if i == index {
                                        RoundedRectangle(cornerRadius: 6).strokeBorder(Theme.accent, lineWidth: 2)
                                    }
                                }
                                .opacity(i == index ? 1 : 0.55)
                        }
                        .buttonStyle(.plain)
                        .id(i)
                        .accessibilityLabel("查看第 \(i + 1) 张图片")
                        .accessibilityAddTraits(i == index ? .isSelected : [])
                    }
                }
                .padding(.horizontal, 12)
                .frame(minWidth: stripWidth)
            }
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { stripWidth = $0 }
            .onChange(of: index, initial: true) { _, current in
                withAnimation { proxy.scrollTo(current, anchor: .center) }
            }
        }
        .accessibilityIdentifier("lightbox-thumbnails")
    }
}

/// 可缩放的单张图：双指缩放、放大后拖动、双击在 1× / 2.5× 之间切换；
/// 有原图地址时，一放大就在舞台图上叠加原图（加载完成前仍显示舞台图）
private struct ZoomableImage: View {
    let url: URL?
    var originalURL: URL?
    var brokenHint: String?
    @State private var scale: CGFloat = 1
    @State private var lastScale: CGFloat = 1
    @State private var offset: CGSize = .zero
    @State private var lastOffset: CGSize = .zero

    var body: some View {
        RemoteImage(url: url, contentMode: .fit, placeholderSymbol: "photo")
            .overlay {
                if scale > 1, let originalURL, originalURL != url {
                    // 加载完成前透明，底下的舞台图照常可见
                    LazyImage(url: originalURL) { state in
                        state.image?.resizable().aspectRatio(contentMode: .fit)
                    }
                }
            }
            .scaleEffect(scale)
            .offset(offset)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .overlay(alignment: .bottom) {
                if url == nil, let brokenHint {
                    Text(brokenHint).font(.caption).foregroundStyle(.white.opacity(0.7)).padding(.bottom, 80)
                }
            }
            .contentShape(.rect)
            .gesture(
                MagnifyGesture()
                    .onChanged { value in scale = max(1, min(lastScale * value.magnification, 5)) }
                    .onEnded { _ in
                        lastScale = scale
                        if scale <= 1 { withAnimation { offset = .zero; lastOffset = .zero } }
                    }
            )
            .simultaneousGesture(
                DragGesture()
                    .onChanged { value in
                        guard scale > 1 else { return }
                        offset = CGSize(width: lastOffset.width + value.translation.width, height: lastOffset.height + value.translation.height)
                    }
                    .onEnded { _ in lastOffset = offset },
                including: scale > 1 ? .all : .subviews
            )
            .onTapGesture(count: 2) {
                withAnimation(.spring(duration: 0.3)) {
                    if scale > 1 {
                        scale = 1; lastScale = 1; offset = .zero; lastOffset = .zero
                    } else {
                        scale = 2.5; lastScale = 2.5
                    }
                }
            }
    }
}

private extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
