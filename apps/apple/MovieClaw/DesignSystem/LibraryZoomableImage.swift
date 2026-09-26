import Nuke
import SwiftUI
import UIKit

/// 可缩放的单张大图：媒体库三个灯箱（图床浏览、照片库、章节图）共用的舞台内核。
///
/// 对应 Web `components/zoom-lightbox.tsx` 的「舞台」部分。设计取舍：
/// - **缩放交给 UIScrollView**：捏合以两指中点为锚、双击在点按处放大到 2.5×（再双击复位）、
///   放大后拖拽平移、缩放上限 5×——这些 Web 端要自己用 Pointer Events 判定的手势，
///   UIScrollView 原生就有，而且与外层分页（TabView）的手势天然协调：未放大时横滑翻页，
///   放大后先平移到边缘、再继续滑才翻页，与系统相册一致；
/// - **三级渐进加载**：先铺墙上那张缩略图（模糊）→ 屏幕适配图盖上 → 只有放大后才拉原图
///   （`fullURL`，照片库才有；图廊与章节图只有一级）。换级时保持当前缩放，不跳；
/// - 屏幕适配图失败而有原图时直接退到原图（派生失败，如 Pillow 不认的格式），
///   都失败才显示「图片加载失败」+ 解释文案；
/// - 单击交给调用方（灯箱据此收放控件），与双击互斥（等双击判定失败才算单击）。
struct LibraryZoomableImage: View {
    /// 墙上那张缩略图：先模糊铺底，屏幕适配图到达前看到大致颜色
    var thumbURL: URL?
    /// 屏幕适配图（主图）；nil = 没有可显示的图
    var screenURL: URL?
    /// 放大后才拉的原图；不给则只有一级
    var fullURL: URL?
    /// 图加载失败时的第二行解释：本地库是文件没了，外链是图床失效
    var brokenHint: String = "文件可能已被移动或删除，重新扫描后会更新"
    /// 是否在画面顶部显示「正在加载 / 正在加载原图」（灯箱有自己的提示时关掉）
    var showsStatus: Bool = true
    /// 单击画面（灯箱用来收放控件）
    var onTap: () -> Void = {}

    @State private var thumb: UIImage?
    @State private var screen: UIImage?
    @State private var full: UIImage?
    @State private var screenFailed = false
    @State private var wantsFull = false
    @State private var fullFailed = false

    private var displayed: UIImage? { full ?? screen ?? thumb }
    private var fullReady: Bool { screen != nil || full != nil }
    private var broken: Bool {
        (screenURL == nil && fullURL == nil) || (screenFailed && (fullURL == nil || fullFailed))
    }

    /// 「正在加载原图」只在真有原图可等时显示：只有一级图的放大后没有第三级，
    /// 不能挂着一条永远消不掉的提示
    private var statusText: String? {
        guard !broken else { return nil }
        if !fullReady { return "正在加载" }
        if fullURL != nil, wantsFull, full == nil, !fullFailed { return "正在加载原图" }
        return nil
    }

    var body: some View {
        ZStack {
            if broken {
                VStack(spacing: 6) {
                    Text("图片加载失败")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.6))
                    Text(brokenHint)
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.4))
                        .multilineTextAlignment(.center)
                }
                .padding(.horizontal, 32)
                .padding(.vertical, 40)
                .background(.white.opacity(0.04), in: .rect(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(.white.opacity(0.12)))
                .padding(24)
                .contentShape(.rect)
                .onTapGesture(perform: onTap)
            } else {
                ZoomingScrollView(image: displayed, onTap: onTap) { scale in
                    // 放大到超过屏幕适配图的分辨率时才拉原图（第三级）
                    if scale > 1.01, fullURL != nil, !wantsFull { wantsFull = true }
                }
                // 只有缩略图垫底时模糊一下，示意「还在加载」
                .blur(radius: fullReady ? 0 : 6)
                .animation(.easeOut(duration: 0.25), value: fullReady)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .top) {
            if showsStatus, let statusText {
                Text(statusText)
                    .font(.caption2)
                    .tracking(0.5)
                    .foregroundStyle(.white.opacity(0.55))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 3)
                    .background(.black.opacity(0.5), in: .capsule)
                    .padding(.top, 64)
                    .allowsHitTesting(false)
            }
        }
        .task(id: screenURL) { await loadPrimary() }
        .task(id: wantsFull) { await loadFull() }
    }

    /// 缩略图与屏幕适配图并行取：缩略图多半已在内存缓存里（墙上刚显示过），立刻就能垫底
    private func loadPrimary() async {
        screen = nil
        screenFailed = false
        async let thumbImage = Self.fetch(thumbURL)
        async let screenImage = Self.fetch(screenURL)
        if let image = await thumbImage { thumb = image }
        if let image = await screenImage {
            screen = image
        } else if !Task.isCancelled {
            screenFailed = true
            // 派生失败：有原图就直接退到原图
            if fullURL != nil { wantsFull = true }
        }
    }

    private func loadFull() async {
        guard wantsFull, full == nil, let fullURL else { return }
        if let image = await Self.fetch(fullURL) {
            full = image
        } else if !Task.isCancelled {
            fullFailed = true
        }
    }

    /// 走 Nuke 同一条管线（共享磁盘缓存与会话 Cookie）；失败返回 nil
    private static func fetch(_ url: URL?) async -> UIImage? {
        guard let url else { return nil }
        return try? await ImagePipeline.shared.image(for: url)
    }
}

/// UIScrollView 承载的缩放舞台。图片按「适应屏幕」排版（四周留 12pt 呼吸），
/// 缩放时保持居中；图片换级（缩略图 → 屏幕图 → 原图）比例不变时不重置缩放。
private struct ZoomingScrollView: UIViewRepresentable {
    let image: UIImage?
    let onTap: () -> Void
    let onZoom: (CGFloat) -> Void

    func makeUIView(context: Context) -> ZoomScrollView {
        let view = ZoomScrollView()
        view.setImage(image)
        return view
    }

    func updateUIView(_ view: ZoomScrollView, context: Context) {
        view.onSingleTap = onTap
        view.onZoomChange = onZoom
        view.setImage(image)
    }
}

private final class ZoomScrollView: UIScrollView, UIScrollViewDelegate {
    private let imageView = UIImageView()
    private var lastBoundsSize: CGSize = .zero
    var onSingleTap: (() -> Void)?
    var onZoomChange: ((CGFloat) -> Void)?

    /// 双击放大到的倍率（同 Web DOUBLE_TAP_ZOOM）
    private static let doubleTapZoom: CGFloat = 2.5
    /// 适应屏幕时四周留的呼吸边距
    private static let breathing: CGFloat = 12

    init() {
        super.init(frame: .zero)
        delegate = self
        minimumZoomScale = 1
        maximumZoomScale = 5
        bouncesZoom = true
        showsVerticalScrollIndicator = false
        showsHorizontalScrollIndicator = false
        contentInsetAdjustmentBehavior = .never
        decelerationRate = .fast
        backgroundColor = .clear
        imageView.contentMode = .scaleAspectFit
        imageView.layer.cornerRadius = 8
        imageView.clipsToBounds = true
        imageView.isUserInteractionEnabled = false
        addSubview(imageView)

        let doubleTap = UITapGestureRecognizer(target: self, action: #selector(handleDoubleTap(_:)))
        doubleTap.numberOfTapsRequired = 2
        addGestureRecognizer(doubleTap)
        let singleTap = UITapGestureRecognizer(target: self, action: #selector(handleSingleTap))
        singleTap.require(toFail: doubleTap)
        addGestureRecognizer(singleTap)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func setImage(_ image: UIImage?) {
        guard image !== imageView.image else { return }
        let previous = imageView.image
        imageView.image = image
        // 换级（同一张图的不同分辨率）比例一致：保持当前缩放与位置
        if let previous, let image, abs(Self.aspect(previous) - Self.aspect(image)) < 0.01 { return }
        resetLayout()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        if bounds.size != lastBoundsSize {
            lastBoundsSize = bounds.size
            resetLayout()
        }
    }

    private static func aspect(_ image: UIImage) -> CGFloat {
        image.size.height > 0 ? image.size.width / image.size.height : 1
    }

    /// 回到适应屏幕：图片等比缩进可视区（扣掉呼吸边距）
    private func resetLayout() {
        zoomScale = 1
        guard let image = imageView.image, bounds.width > 0, bounds.height > 0 else {
            imageView.frame = .zero
            contentSize = .zero
            return
        }
        let box = CGSize(width: max(bounds.width - Self.breathing * 2, 1), height: max(bounds.height - Self.breathing * 2, 1))
        let aspect = Self.aspect(image)
        var size = CGSize(width: box.width, height: box.width / aspect)
        if size.height > box.height { size = CGSize(width: box.height * aspect, height: box.height) }
        imageView.frame = CGRect(origin: .zero, size: size)
        contentSize = size
        centerContent()
    }

    /// 内容比可视区小时用 inset 居中（缩放过程中持续调整）
    private func centerContent() {
        let dx = max(0, (bounds.width - contentSize.width) / 2)
        let dy = max(0, (bounds.height - contentSize.height) / 2)
        contentInset = UIEdgeInsets(top: dy, left: dx, bottom: dy, right: dx)
    }

    func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }

    func scrollViewDidZoom(_ scrollView: UIScrollView) {
        centerContent()
        onZoomChange?(zoomScale)
    }

    @objc private func handleDoubleTap(_ gesture: UITapGestureRecognizer) {
        if zoomScale > minimumZoomScale + 0.01 {
            setZoomScale(minimumZoomScale, animated: true)
            return
        }
        let point = gesture.location(in: imageView)
        let scale = Self.doubleTapZoom
        let size = CGSize(width: bounds.width / scale, height: bounds.height / scale)
        zoom(to: CGRect(x: point.x - size.width / 2, y: point.y - size.height / 2, width: size.width, height: size.height), animated: true)
    }

    @objc private func handleSingleTap() {
        onSingleTap?()
    }
}

// MARK: - 灯箱外壳

extension LibraryZoomableImage {
    /// 灯箱里的一张：三级图片地址 + 缩略条上的比例（同 Web ZoomLightboxSlide）
    struct Slide: Identifiable {
        var id: Int
        var title: String
        var thumbURL: URL?
        var screenURL: URL?
        var fullURL: URL?
        /// 缩略条上的宽高比；拿不到尺寸按 1:1
        var aspect: Double = 1
    }

    /// 全屏灯箱外壳（对应 Web ZoomLightbox 的顶栏 / 底栏 / 翻页）：
    /// - 横滑翻页（TabView 分页），放大时先平移、到边缘才翻页；
    /// - 单击画面收放控件（顶栏计数 + 标题 + 调用方的工具键 + 关闭，底栏缩略条），
    ///   收起后画面独占整屏；与 Web 一样不做闲置自动淡出；
    /// - 翻到已加载列表末尾前 5 张时向外要下一页（`onReachEnd`），拿到后继续翻；
    /// - 触屏不摆缩放按钮与翻页箭头（Web 也只给有鼠标的设备）。
    ///
    /// 由调用方用 `.fullScreenCover` 呈现；关闭走 `dismiss`。
    struct Lightbox<Actions: View, Overlay: View>: View {
        let slides: [Slide]
        @Binding var index: Int
        var hasMore: Bool = false
        var onReachEnd: () -> Void = {}
        /// 画面顶部的提示（下载进度、操作失败等）；有它时隐藏加载提示
        var note: String?
        var brokenHint: String = "文件可能已被移动或删除，重新扫描后会更新"
        @ViewBuilder var actions: () -> Actions
        /// 压在画面上的浮层（信息面板等），跟着控件一起收放
        @ViewBuilder var overlay: () -> Overlay

        @Environment(\.dismiss) private var dismiss
        @State private var chromeShown = true

        var body: some View {
            ZStack {
                Color(red: 4 / 255, green: 5 / 255, blue: 9 / 255).opacity(0.97).ignoresSafeArea()
                TabView(selection: $index) {
                    ForEach(slides.indices, id: \.self) { i in
                        LibraryZoomableImage(
                            thumbURL: slides[i].thumbURL,
                            screenURL: slides[i].screenURL,
                            fullURL: slides[i].fullURL,
                            brokenHint: brokenHint,
                            showsStatus: note == nil,
                            onTap: { withAnimation(.easeInOut(duration: 0.25)) { chromeShown.toggle() } }
                        )
                        .tag(i)
                    }
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
                .ignoresSafeArea()

                if chromeShown {
                    VStack(spacing: 0) {
                        topBar
                        overlay()
                        Spacer(minLength: 0)
                        thumbnailStrip
                    }
                    .transition(.opacity)
                }
            }
            .overlay(alignment: .top) {
                if let note {
                    Text(note)
                        .font(.caption2)
                        .foregroundStyle(.white.opacity(0.7))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 3)
                        .background(.black.opacity(0.55), in: .capsule)
                        .padding(.top, 64)
                        .allowsHitTesting(false)
                }
            }
            .preferredColorScheme(.dark)
            .statusBarHidden(!chromeShown)
            // 快翻到末尾前提前要下一页（调用方自己挡重复请求），翻到最后一张时通常已经到了
            .onChange(of: index, initial: true) { requestMoreIfNeeded() }
            .onChange(of: slides.count) { requestMoreIfNeeded() }
        }

        private func requestMoreIfNeeded() {
            if hasMore, index >= slides.count - 5 { onReachEnd() }
        }

        private var counter: String {
            "\(min(index + 1, slides.count)) / \(hasMore ? "\(slides.count)+" : "\(slides.count)")"
        }

        private var topBar: some View {
            HStack(spacing: 8) {
                Text(counter)
                    .font(.subheadline.monospacedDigit())
                    .padding(.horizontal, 10)
                    .padding(.vertical, 3)
                    .background(.white.opacity(0.1), in: .capsule)
                Text(slides.indices.contains(index) ? slides[index].title : "")
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.7))
                    .lineLimit(1)
                    .frame(maxWidth: .infinity)
                HStack(spacing: 2) {
                    actions()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 18, weight: .semibold))
                            .frame(width: 44, height: 44)
                            .contentShape(.circle)
                    }
                    .accessibilityLabel("关闭")
                }
                .foregroundStyle(.white.opacity(0.85))
                .buttonStyle(.plain)
            }
            .foregroundStyle(.white.opacity(0.85))
            .padding(.horizontal, 12)
            .padding(.bottom, 24)
            .background(
                LinearGradient(colors: [Color.black.opacity(0.85), Color.black.opacity(0.4), .clear], startPoint: .top, endPoint: .bottom)
                    .ignoresSafeArea(edges: .top)
            )
        }

        /// 底部缩略条：当前张高亮并自动居中，点击直达
        private var thumbnailStrip: some View {
            ScrollViewReader { proxy in
                ScrollView(.horizontal, showsIndicators: false) {
                    LazyHStack(spacing: 6) {
                        ForEach(slides.indices, id: \.self) { i in
                            let slide = slides[i]
                            Button { index = i } label: {
                                RemoteImage(url: slide.thumbURL, placeholderSymbol: "photo")
                                    .frame(width: 48 * min(2, max(0.5, slide.aspect)), height: 48)
                                    .clipShape(.rect(cornerRadius: 6))
                                    .overlay {
                                        if i == index {
                                            RoundedRectangle(cornerRadius: 6).strokeBorder(Theme.accent, lineWidth: 2)
                                        }
                                    }
                                    .opacity(i == index ? 1 : 0.45)
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("查看 \(slide.title)")
                            .id(i)
                        }
                    }
                    .padding(.horizontal, 12)
                    .padding(.top, 36)
                    .padding(.bottom, 10)
                }
                .onChange(of: index, initial: true) { _, newValue in
                    withAnimation { proxy.scrollTo(newValue, anchor: .center) }
                }
            }
            .frame(height: 94)
            .background(
                LinearGradient(colors: [.clear, Color.black.opacity(0.5), Color.black.opacity(0.9)], startPoint: .top, endPoint: .bottom)
                    .ignoresSafeArea(edges: .bottom)
            )
        }
    }
}

extension LibraryZoomableImage.Lightbox where Overlay == EmptyView {
    init(
        slides: [LibraryZoomableImage.Slide],
        index: Binding<Int>,
        hasMore: Bool = false,
        onReachEnd: @escaping () -> Void = {},
        note: String? = nil,
        brokenHint: String = "文件可能已被移动或删除，重新扫描后会更新",
        @ViewBuilder actions: @escaping () -> Actions
    ) {
        self.init(slides: slides, index: index, hasMore: hasMore, onReachEnd: onReachEnd, note: note, brokenHint: brokenHint, actions: actions, overlay: { EmptyView() })
    }
}

extension LibraryZoomableImage {
    /// 灯箱顶栏的图标键（与关闭键同一形状：44pt 可点区域）
    struct ActionButton: View {
        let systemImage: String
        let label: String
        var tint: Color = .white.opacity(0.85)
        let action: () -> Void

        var body: some View {
            Button(action: action) {
                Image(systemName: systemImage)
                    .font(.system(size: 19, weight: .medium))
                    .foregroundStyle(tint)
                    .frame(width: 44, height: 44)
                    .contentShape(.circle)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(label)
        }
    }
}
