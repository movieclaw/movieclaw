import GLKit
import Libmpv
import UIKit

/// Metal 渲染表面：mpv 的 gpu-next（经 MoltenVK）直接往这个 CAMetalLayer 上画。
///
/// 我们只负责跟随视图尺寸更新 layer 的 frame 与像素密度，绘制完全在 mpv 的渲染线程里。
final class MPVMetalView: UIView {
    let metalLayer = MPVMetalLayer()
    /// 像素尺寸变化（含首次拿到有效尺寸、旋转、分屏）时回调。
    /// mpv 的 moltenvk 上下文只在视频输出初始化/配置时读取 drawableSize，之后的尺寸变化它感知不到，需要外部触发。
    var onDrawableSizeChange: ((CGSize) -> Void)?
    private var lastDrawableSize: CGSize = .zero

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        metalLayer.framebufferOnly = true
        metalLayer.backgroundColor = UIColor.black.cgColor
        layer.addSublayer(metalLayer)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layoutSubviews() {
        super.layoutSubviews()
        let scale = window?.screen.nativeScale ?? traitCollection.displayScale
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        metalLayer.frame = bounds
        metalLayer.contentsScale = scale
        // CAMetalLayer 不会随 frame 自动改像素尺寸：必须显式同步 drawableSize，
        // 否则转横屏后 mpv（MoltenVK 交换链按 drawableSize 建）仍按竖屏尺寸出图，
        // 画面被压扁/偏到一角（真机横竖屏切换实测）。尺寸变化后交换链失效，mpv 会自行重建。
        let size = CGSize(width: bounds.width * scale, height: bounds.height * scale)
        metalLayer.drawableSize = size
        CATransaction.commit()
        guard size.width > 1, size.height > 1, size != lastDrawableSize else { return }
        lastDrawableSize = size
        onDrawableSizeChange?(size)
    }
}

/// MoltenVK 的两个已知坑（MPVKit 示例同款修正）：
/// 1. 呈现时会把 drawableSize 临时设成 1x1，导致闪烁甚至停在 1x1——忽略这种设置；
/// 2. HDR 需要在主线程改 wantsExtendedDynamicRangeContent 才能真正打开屏幕的 EDR。
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
                let box = UncheckedBox(self)
                DispatchQueue.main.sync { box.value.setSuperEDR(newValue) }
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
