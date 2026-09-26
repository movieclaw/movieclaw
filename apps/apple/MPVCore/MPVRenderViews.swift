import GLKit
import Libmpv
import UIKit

/// Metal 画面容器：黑底铺满播放区域，真正的渲染面（MPVMetalView）按视频比例居中摆在里面。
///
/// ## 渲染面只占画面那一块：横竖屏切换全程不变形、不黑屏
/// 渲染面的宽高比始终等于视频的宽高比。旋转时 UIKit 的旋转动画让它的 frame 从「竖屏画面区」连续变到
/// 「横屏画面区」，两端比例相同、中间每一帧也相同：mpv 还没按新尺寸出图时，旧帧被等比缩放着显示，
/// 位置和比例全程正确；mpv（打了尺寸自检补丁）一两帧后按新尺寸出图，只是让画面变清晰，位置不跳。
/// 以前渲染面铺满视图：mpv 跟上之前旧帧被拉伸到新的屏幕比例（压扁、偏到一边），只能先藏画面再等 mpv。
///
/// 为什么不让渲染面铺满、只把 drawableSize 设成画面大小：MoltenVK 1.4 重建交换链时按 layer 的
/// bounds × contentsScale 取尺寸，随即把 drawableSize 改回整个视图（模拟器 Metal 路径实测）。
///
/// 视频比例未知时（起播前、纯音频）渲染面铺满，由 mpv 自己加黑边。
/// 连带效果：mpv 的字幕画布就是画面区域，字号与底边距按「画面高度」计——
/// 与网页、AVPlayer 路径（SubtitleOverlay 锚定画面矩形）的字幕口径一致。
final class MPVMetalContainerView: UIView {
    let pictureView = MPVMetalView()
    /// 视频显示尺寸（已计入像素宽高比与旋转元数据）；.zero = 还不知道
    var videoSize: CGSize = .zero {
        didSet { if videoSize != oldValue { setNeedsLayout() } }
    }

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        addSubview(pictureView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layoutSubviews() {
        super.layoutSubviews()
        // 旋转时这里在系统的旋转动画里执行：frame 变化随动画走，渲染面等比缩放
        pictureView.frame = Self.pictureFrame(video: videoSize, in: bounds)
    }

    /// 画面区：视频按原比例放进容器（aspect-fit）并居中；边长取整到点，出图像素尺寸因此是整数
    nonisolated static func pictureFrame(video: CGSize, in bounds: CGRect) -> CGRect {
        guard video.width > 0, video.height > 0, bounds.width > 0, bounds.height > 0 else { return bounds }
        let fit = min(bounds.width / video.width, bounds.height / video.height)
        let size = CGSize(width: (video.width * fit).rounded(), height: (video.height * fit).rounded())
        return CGRect(
            x: ((bounds.width - size.width) / 2).rounded(),
            y: ((bounds.height - size.height) / 2).rounded(),
            width: size.width,
            height: size.height
        )
    }
}

/// Metal 渲染表面：mpv 的 gpu-next（经 MoltenVK）直接往这个视图自己的 CAMetalLayer 上画。
///
/// 绘制完全在 mpv 的渲染线程里；摆放由 MPVMetalContainerView 按视频比例决定，这里只管出图像素尺寸。
final class MPVMetalView: UIView {
    override class var layerClass: AnyClass { MPVMetalLayer.self }
    /// 视图自己的 layer（不是另挂的子层）：尺寸变化才会跟着 UIKit 的旋转动画一起走
    var metalLayer: MPVMetalLayer { layer as! MPVMetalLayer }
    /// 出图像素尺寸变化（含首次拿到有效尺寸、旋转、视频比例确定）时回调
    var onDrawableSizeChange: ((CGSize) -> Void)?
    private var lastDrawableSize: CGSize = .zero

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        // 旧帧与新尺寸比例有细微出入（取整）时按比例缩放而不是拉伸：contentMode 决定 layer 的 contentsGravity
        contentMode = .scaleAspectFit
        metalLayer.framebufferOnly = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    /// 隐藏/显示画面（兜底重建视频输出期间隐藏，避免露出错位的过渡帧）；显示时短暂淡入
    func setPictureHidden(_ hidden: Bool) {
        guard (alpha == 0) != hidden else { return }
        if hidden {
            alpha = 0
        } else {
            UIView.animate(withDuration: 0.15) { self.alpha = 1 }
        }
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        let scale = window?.screen.nativeScale ?? traitCollection.displayScale
        if contentScaleFactor != scale { contentScaleFactor = scale }
        // CAMetalLayer 不会随 bounds 自动改像素尺寸，要显式设 drawableSize（与 MoltenVK 取的 bounds × contentsScale 一致）。
        // 旋转时这里拿到的已是动画终点的 bounds：出图尺寸一步到位，动画过程交给层的等比缩放
        let size = CGSize(width: bounds.width * scale, height: bounds.height * scale)
        guard size.width > 1, size.height > 1, size != lastDrawableSize else { return }
        lastDrawableSize = size
        metalLayer.drawableSize = size
        onDrawableSizeChange?(size)
    }
}

/// MoltenVK 的两个已知坑（MPVKit 示例同款修正）：
/// 1. 呈现时会把 drawableSize 临时设成 1x1，导致闪烁甚至停在 1x1——忽略这种设置；
/// 2. HDR 需要在主线程改 wantsExtendedDynamicRangeContent 才能真正打开屏幕的 EDR（异步切过去，见 setter）。
nonisolated final class MPVMetalLayer: CAMetalLayer {
    override var drawableSize: CGSize {
        get { super.drawableSize }
        set {
            if Int(newValue.width) > 1, Int(newValue.height) > 1 {
                super.drawableSize = newValue
            }
        }
    }

    override var wantsExtendedDynamicRangeContent: Bool {
        get { super.wantsExtendedDynamicRangeContent }
        set {
            if Thread.isMainThread {
                super.wantsExtendedDynamicRangeContent = newValue
            } else {
                // 异步切回主线程（MPVKit 示例用的是 sync）：vo 线程在这里同步等主线程，而主线程
                // 若恰好在等 mpv 的核心锁（读属性、发命令），两边互等就是死锁——界面整个点不动
                let box = UncheckedBox(self)
                DispatchQueue.main.async { box.value.setSuperEDR(newValue) }
            }
        }
    }

    private func setSuperEDR(_ value: Bool) {
        super.wantsExtendedDynamicRangeContent = value
    }
}

/// OpenGL ES 渲染表面（模拟器兜底）：通过 libmpv 的 render API 把每一帧画进 GLKView 的帧缓冲。
///
/// mpv 有新帧时回调 update → 主线程 `setNeedsDisplay` → `draw(_:)` 里 `mpv_render_context_render`。
/// 注意：OpenGL ES 路径不能正确显示 10bit 视频（mpv #7846），所以真机默认走 Metal。
final class MPVGLView: GLKView {
    private var renderContext: OpaquePointer?
    private var defaultFBO: GLint = -1
    /// 回调上下文：弱引用视图，视图先释放时排队中的重绘直接作废
    private lazy var updateTarget = MPVGLUpdateTarget(view: self)

    init() {
        let context = EAGLContext(api: .openGLES3) ?? EAGLContext(api: .openGLES2)!
        super.init(frame: .zero, context: context)
        backgroundColor = .black
        enableSetNeedsDisplay = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        contentScaleFactor = window?.screen.nativeScale ?? traitCollection.displayScale
    }

    /// 在 mpv_initialize 之后创建 render context
    func attach(to mpv: OpaquePointer) throws {
        EAGLContext.setCurrent(context)
        let apiType = UnsafeMutableRawPointer(mutating: (MPV_RENDER_API_TYPE_OPENGL as NSString).utf8String)
        var initParams = mpv_opengl_init_params(
            get_proc_address: mpvGLGetProcAddress,
            get_proc_address_ctx: nil
        )
        var created: OpaquePointer?
        let status = withUnsafeMutablePointer(to: &initParams) { initPointer -> Int32 in
            var params = [
                mpv_render_param(type: MPV_RENDER_PARAM_API_TYPE, data: apiType),
                mpv_render_param(type: MPV_RENDER_PARAM_OPENGL_INIT_PARAMS, data: initPointer),
                mpv_render_param(),
            ]
            return mpv_render_context_create(&created, mpv, &params)
        }
        guard status >= 0, let created else {
            throw MPVError.renderInitFailed(String(cString: mpv_error_string(status)))
        }
        renderContext = created
        mpv_render_context_set_update_callback(created, mpvGLUpdate, Unmanaged.passUnretained(updateTarget).toOpaque())
    }

    /// 在 mpv 销毁前释放 render context（必须在 GL 上下文当前时）
    func detach() {
        guard let renderContext else { return }
        EAGLContext.setCurrent(context)
        mpv_render_context_set_update_callback(renderContext, nil, nil)
        mpv_render_context_free(renderContext)
        self.renderContext = nil
    }

    override func draw(_ rect: CGRect) {
        guard let renderContext else { return }
        glClearColor(0, 0, 0, 1)
        glClear(GLbitfield(GL_COLOR_BUFFER_BIT))
        glGetIntegerv(GLenum(GL_FRAMEBUFFER_BINDING), &defaultFBO)
        var dims: [GLint] = [0, 0, 0, 0]
        glGetIntegerv(GLenum(GL_VIEWPORT), &dims)
        var fbo = mpv_opengl_fbo(fbo: Int32(defaultFBO), w: Int32(dims[2]), h: Int32(dims[3]), internal_format: 0)
        var flip: Int32 = 1
        withUnsafeMutablePointer(to: &fbo) { fboPointer in
            withUnsafeMutablePointer(to: &flip) { flipPointer in
                var params = [
                    mpv_render_param(type: MPV_RENDER_PARAM_OPENGL_FBO, data: fboPointer),
                    mpv_render_param(type: MPV_RENDER_PARAM_FLIP_Y, data: flipPointer),
                    mpv_render_param(),
                ]
                mpv_render_context_render(renderContext, &params)
            }
        }
    }
}

// MARK: - C 回调（必须非隔离：mpv 在自己的 vo 线程上调用它们）

/// mpv 有新帧：切回主线程让 GLKView 重绘
nonisolated private func mpvGLUpdate(_ raw: UnsafeMutableRawPointer?) {
    guard let raw else { return }
    let target = Unmanaged<MPVGLUpdateTarget>.fromOpaque(raw).takeUnretainedValue()
    DispatchQueue.main.async {
        MainActor.assumeIsolated { target.view?.setNeedsDisplay() }
    }
}

nonisolated final class MPVGLUpdateTarget: @unchecked Sendable {
    weak var view: MPVGLView?
    init(view: MPVGLView) { self.view = view }
}

/// OpenGL ES 函数地址解析
nonisolated private func mpvGLGetProcAddress(_: UnsafeMutableRawPointer?, _ name: UnsafePointer<CChar>?) -> UnsafeMutableRawPointer? {
    let symbol = CFStringCreateWithCString(kCFAllocatorDefault, name, CFStringBuiltInEncodings.ASCII.rawValue)
    let bundle = CFBundleGetBundleWithIdentifier("com.apple.opengles" as CFString)
    return CFBundleGetFunctionPointerForName(bundle, symbol)
}
