import Foundation
import Libmpv
import UIKit

/// libmpv 的 Swift 封装，是 App 与 LGPL 组件之间**唯一**的边界。
///
/// ## 为什么单独做成动态框架（MPVCore.framework）
/// MPVKit 发布的 xcframework 全是静态库。静态链进 App 主二进制的话，LGPL 要求
/// 我们额外提供可重新链接的目标文件；把 libmpv / FFmpeg 全部收进一个**动态框架**，
/// 用户可以用自己编译的同名框架替换它（LGPL 2.1 §6 的「合适的共享库机制」）。
/// 因此 App 主体只 import MPVCore、从不直接 import Libmpv —— 这条边界不能破。
///
/// ## 线程模型
/// - mpv 句柄本身线程安全；命令、属性读写可以在主线程直接调；
/// - mpv 通过 wakeup 回调通知「有事件了」（在 mpv 内部线程上），这里把排空事件的
///   工作派到私有串行队列，再把解析好的事件切回主线程交给 `onEvent`；
/// - 销毁时先摘掉 wakeup 回调、在串行队列上 `mpv_terminate_destroy`（它会阻塞到
///   解码/输出线程全部退出），渲染层要活到那一刻之后才能释放。
///
/// ## 渲染
/// 两条路径（见 `MPVRenderBackend`）：真机用 Metal（MoltenVK + gpu-next，支持 10bit/HDR）；
/// 模拟器用 OpenGL ES 渲染 API（libmpv render context）。Debug 下可用 `-mcMPVBackend metal|openGL` 强制。
@MainActor
public final class MPVPlayer {
    /// 渲染表面：由调用方塞进视图层级（铺满播放区域）
    public let view: UIView
    /// 实际使用的渲染方式
    public let backend: MPVRenderBackend
    /// 事件回调（主线程）
    public var onEvent: ((MPVEvent) -> Void)?
    /// 即将为尺寸校正重建视频输出（主线程）：重建期间解码重启会掉帧、画面短暂停住，
    /// 上层的卡顿/掉帧看门狗要在这段时间里别误判
    public var onVideoOutputRebuild: (() -> Void)?

    private let handle: MPVHandle
    /// Metal 画面容器与其中的渲染面（OpenGL 路径为 nil）：渲染面按视频比例摆放，mpv 出图要跟上它的尺寸
    private var metalContainer: MPVMetalContainerView?
    private var metalViewForResize: MPVMetalView? { metalContainer?.pictureView }
    /// 视频输出驱动（兜底重建时切回它）
    private let videoOutput: String

    // 以下都由 mpv 的属性事件推送、在主线程缓存——尺寸核对全程不在主线程同步读 mpv（会等核心锁）
    /// 渲染面当前的出图像素尺寸（我们设给 CAMetalLayer 的 drawableSize）
    private var drawableTarget = (width: 0, height: 0)
    /// mpv 实际的出图尺寸（osd-width / osd-height）
    private var outputSize = (width: 0, height: 0)
    /// 视频显示尺寸（dwidth / dheight，已计像素宽高比）与旋转元数据
    private var displaySize = (width: 0, height: 0)
    private var rotation = 0
    /// 正在出视频（有视频轨且 VO 已配置）；关视频轨、纯音频时为 false
    private var hasVideo = false
    /// 出图尺寸对不上时的兜底计时，与兜底重建本身
    private var fallbackTask: Task<Void, Never>?
    private var rebuildTask: Task<Void, Never>?
    #if DEBUG
    private var resizeStartedAt: (width: Int, height: Int, at: ContinuousClock.Instant)?
    #endif

    /// 需要持续观察的属性（变化时推送 `.property` 事件）
    private static let observed: [(String, mpv_format)] = [
        ("time-pos", MPV_FORMAT_DOUBLE),
        ("duration", MPV_FORMAT_DOUBLE),
        ("pause", MPV_FORMAT_FLAG),
        ("paused-for-cache", MPV_FORMAT_FLAG),
        ("seeking", MPV_FORMAT_FLAG),
        ("eof-reached", MPV_FORMAT_FLAG),
        ("demuxer-cache-time", MPV_FORMAT_DOUBLE),
        ("track-list/count", MPV_FORMAT_INT64),
        ("dwidth", MPV_FORMAT_INT64),
        ("dheight", MPV_FORMAT_INT64),
        ("core-idle", MPV_FORMAT_FLAG),
        // 以下三项 MPVCore 自己用：出图尺寸核对、画面比例（见 observeGeometry）
        ("osd-width", MPV_FORMAT_INT64),
        ("osd-height", MPV_FORMAT_INT64),
        ("video-out-params/rotate", MPV_FORMAT_INT64),
    ]

    /// - Parameters:
    ///   - backend: 渲染方式；nil = 按运行环境自动选（真机 Metal，模拟器 OpenGL ES）
    ///   - options: 额外的 mpv 选项（`mpv_initialize` 之前设置）
    public init(backend: MPVRenderBackend? = nil, options: [String: String] = [:]) throws {
        guard let mpv = mpv_create() else { throw MPVError.createFailed }
        let chosen = backend ?? MPVRenderBackend.automatic
        self.backend = chosen
        handle = MPVHandle(mpv: mpv)

        // ---- 通用选项（mpv 手册 https://mpv.io/manual/stable/#options）----
        var base: [String: String] = [
            // 不要 mpv 自己的 OSD / 按键绑定：控制层全在 SwiftUI 里
            "osd-level": "0",
            "input-default-bindings": "no",
            "input-vo-keyboard": "no",
            // 播完停在最后一帧（而不是卸载文件），「即将播放」卡片与片尾状态要用
            "keep-open": "yes",
            // 字幕由我们显式选择，不要按系统语言自动挑
            "sid": "no",
            "sub-auto": "no",
            // libass 找不到 ASS 指定字体时的中文兜底：系统自带的苹方
            "sub-font": "PingFang SC",
            // 网络与缓存：手机内存有限，前向缓存上限 150 MiB、回看 50 MiB
            "cache": "yes",
            "demuxer-max-bytes": "150MiB",
            "demuxer-max-back-bytes": "50MiB",
            "network-timeout": "30",
            "user-agent": "MovieClaw-iOS (libmpv)",
            "audio-client-name": "MovieClaw",
            "hwdec": MPVRenderBackend.isSimulator ? "no" : "videotoolbox",
        ]
        switch chosen {
        case .metal:
            base["vo"] = "gpu-next"
            base["gpu-api"] = "vulkan"
            base["gpu-context"] = "moltenvk"
        case .openGL:
            base["vo"] = "libmpv"
        }
        for (key, value) in options { base[key] = value }
        videoOutput = base["vo"] ?? "gpu-next"

        #if DEBUG
        mpv_request_log_messages(mpv, "warn")
        #else
        mpv_request_log_messages(mpv, "error")
        #endif

        switch chosen {
        case .metal:
            let container = MPVMetalContainerView()
            view = container
            metalContainer = container
            // wid 传的是 CAMetalLayer 的指针（MPVKit 的 moltenvk 补丁约定）
            var layerPointer = Int64(Int(bitPattern: Unmanaged.passUnretained(container.pictureView.metalLayer).toOpaque()))
            mpv_set_option(mpv, "wid", MPV_FORMAT_INT64, &layerPointer)
        case .openGL:
            view = MPVGLView()
        }
        for (key, value) in base {
            let status = mpv_set_option_string(mpv, key, value)
            if status < 0 {
                MPVHandle.log("选项 \(key)=\(value) 设置失败：\(String(cString: mpv_error_string(status)))")
            }
        }

        let initStatus = mpv_initialize(mpv)
        guard initStatus >= 0 else {
            mpv_terminate_destroy(mpv)
            throw MPVError.initializeFailed(String(cString: mpv_error_string(initStatus)))
        }
        if let glView = view as? MPVGLView {
            do {
                try glView.attach(to: mpv)
            } catch {
                mpv_terminate_destroy(mpv)
                throw error
            }
        }

        for (name, format) in Self.observed {
            mpv_observe_property(mpv, 0, name, format)
        }
        handle.sink = { [weak self] event in
            // 已在主线程
            self?.observeGeometry(event)
            self?.onEvent?(event)
        }
        handle.startEventLoop()

        // 渲染面出图尺寸变了（旋转、视频比例确定）：核对 mpv 是否跟上，见 checkOutputSize
        metalViewForResize?.onDrawableSizeChange = { [weak self] size in
            guard let self else { return }
            drawableTarget = (Int(size.width.rounded()), Int(size.height.rounded()))
            #if DEBUG
            resizeStartedAt = (drawableTarget.width, drawableTarget.height, .now)
            let animated = UIView.inheritedAnimationDuration > 0 ? "随旋转动画" : "无动画"
            MPVDiag.log("出图尺寸改为 \(drawableTarget.width)x\(drawableTarget.height)（\(animated)），mpv 当前 \(outputSize.width)x\(outputSize.height)")
            #endif
            // 兜底计时从最近一次尺寸变化重新算：连续快速旋转时，不能拿上一次的计时误判 mpv 没跟上
            fallbackTask?.cancel()
            fallbackTask = nil
            checkOutputSize()
        }
    }

    // MARK: - 画面尺寸（事件驱动）

    /// 从属性事件里取 MPVCore 自己关心的几项：mpv 出图尺寸、视频显示尺寸与旋转。
    /// 只认有效值——关视频轨（切后台）时这些属性变成「不可用」，保留上次的比例，回前台不用重算一轮
    private func observeGeometry(_ event: MPVEvent) {
        guard case let .property(name, value) = event else { return }
        let number: Int? = if case let .int(raw) = value { Int(raw) } else { nil }
        switch name {
        case "osd-width", "osd-height":
            if name == "osd-width" { outputSize.width = number ?? 0 } else { outputSize.height = number ?? 0 }
            checkOutputSize()
        case "dwidth", "dheight":
            if name == "dwidth" { hasVideo = (number ?? 0) > 0 }
            guard let number, number > 0 else { return }
            if name == "dwidth" { displaySize.width = number } else { displaySize.height = number }
            updateVideoSize()
        case "video-out-params/rotate":
            guard let number else { return }
            rotation = number
            updateVideoSize()
        default:
            break
        }
    }

    /// 把视频显示尺寸交给画面容器，由它按比例摆放渲染面。
    /// 带 90°/270° 旋转元数据的视频（手机竖拍）mpv 会转过来画，宽高要对调
    private func updateVideoSize() {
        guard let metalContainer, displaySize.width > 0, displaySize.height > 0 else { return }
        let turned = rotation % 180 != 0
        metalContainer.videoSize = CGSize(
            width: turned ? displaySize.height : displaySize.width,
            height: turned ? displaySize.width : displaySize.height
        )
    }

    /// 核对 mpv 实际出图尺寸是否等于渲染面的 drawableSize。渲染面尺寸变化、mpv 出图尺寸变化时各调一次，纯事件驱动。
    ///
    /// Vendor/MPVKit 里的 libmpv 打了 0004-moltenvk-detect-resize 补丁：drawableSize 一变，mpv 的 VO 线程
    /// 下一帧就自己按新尺寸出图，这里正常只会看到「对上了」。对不上时挂一个兜底计时，
    /// 600ms 后仍对不上（例如换回了没打补丁的上游 libmpv）才重建视频输出。
    /// 画面位置不依赖 mpv 何时跟上（见 MPVMetalView），所以兜底不必抢时间。
    private func checkOutputSize() {
        let target = drawableTarget, output = outputSize
        // 渲染面还没排版、mpv 还没出过图：等下一次事件
        guard target.width > 0, output.width > 0, output.height > 0 else { return }
        if Self.sizesMatch(output, target) {
            fallbackTask?.cancel()
            fallbackTask = nil
            #if DEBUG
            if let started = resizeStartedAt, started.width == target.width, started.height == target.height {
                let elapsed = ContinuousClock.now - started.at
                MPVDiag.log("mpv 跟上新尺寸 \(target.width)x\(target.height)：\(Int(elapsed / .milliseconds(1))) 毫秒")
                resizeStartedAt = nil
            }
            #endif
            return
        }
        guard hasVideo, fallbackTask == nil, rebuildTask == nil else { return }
        fallbackTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(600))
            guard !Task.isCancelled, let self else { return }
            fallbackTask = nil
            rebuildVideoOutput()
        }
    }

    /// 出图尺寸与目标一致（容差 1 像素：像素密度非整数的机型上，MoltenVK 与我们对 bounds × scale 的取整可能差 1）
    private static func sizesMatch(_ a: (width: Int, height: Int), _ b: (width: Int, height: Int)) -> Bool {
        abs(a.width - b.width) <= 1 && abs(a.height - b.height) <= 1
    }

    /// 兜底：重建视频输出（VO），让没跟上新尺寸的 mpv 按新尺寸出图。
    ///
    /// 只有换回没打补丁的上游 libmpv 时才会走到这里。上游 iOS 版 libmpv 只在 VO 初始化/视频配置时
    /// 读一次 drawableSize，没有外部尺寸通知（android-surface-size 设置返回 -12），video-reload 在参数不变时
    /// 跳过配置，改 video-aspect-override / video-rotate 也不重算输出尺寸（真机逐一实测过）。
    /// 可行的只有重建 VO，两种做法真机测速（iPhone Air，4K 杜比视界）：
    /// - 切 vo 到 null 再切回：只重建输出、保留解码器，约 0.6 秒——先用它；
    /// - 关开视频轨：连解码器一起重启、要从关键帧重新拉 4K 数据，约 1.2 秒——作为兜底。
    /// 期间把画面隐藏、完成再淡入：用户看到的是短暂黑一下，而不是错位的画面。
    private func rebuildVideoOutput() {
        let target = drawableTarget
        guard let metalView = metalViewForResize, hasVideo, rebuildTask == nil, !Self.sizesMatch(outputSize, target) else { return }
        rebuildTask = Task { [weak self] in
            guard let self else { return }
            #if DEBUG
            let started = ContinuousClock.now
            #endif
            metalView.setPictureHidden(true)
            onVideoOutputRebuild?()

            // 快路径：只重建视频输出
            setString("vo", "null")
            try? await Task.sleep(for: .milliseconds(30))
            guard !Task.isCancelled else { return }
            setString("vo", videoOutput)
            var fixed = await waitForOutput(target, timeout: .milliseconds(1500))

            // 兜底：关开视频轨（连解码器一起重启）。App 只在前后台切换时关视频轨，重开一律 auto
            if !fixed, !Task.isCancelled {
                onVideoOutputRebuild?()
                setString("vid", "no")
                try? await Task.sleep(for: .milliseconds(60))
                guard !Task.isCancelled else { return }
                setString("vid", "auto")
                fixed = await waitForOutput(target, timeout: .milliseconds(3000))
            }
            #if DEBUG
            MPVDiag.log("兜底重建视频输出\(fixed ? "完成" : "未完成")：\(Int((ContinuousClock.now - started) / .milliseconds(1))) 毫秒，目标 \(target.width)x\(target.height)")
            #endif
            guard !Task.isCancelled else { return }
            rebuildTask = nil
            metalView.setPictureHidden(false)
        }
    }

    /// 等 mpv 出图尺寸变成目标值（看事件推送的缓存值，不读 mpv）
    private func waitForOutput(_ target: (width: Int, height: Int), timeout: Duration) async -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(30))
            if Task.isCancelled { return false }
            if Self.sizesMatch(outputSize, target) { return true }
        }
        return false
    }

    // MARK: - 播放控制

    /// 打开一个地址。`start` 是起播秒数（mpv 的 start 选项，对下一个文件生效）。
    public func load(_ url: URL, start: Double?, paused: Bool) {
        setString("start", start.map { String(format: "%.3f", max(0, $0)) } ?? "none")
        setFlag("pause", paused)
        command(["loadfile", url.absoluteString, "replace"])
    }

    public func play() { setFlag("pause", false) }
    public func pause() { setFlag("pause", true) }

    /// 绝对定位（秒）。`exact` = 精确到帧（拖进度条松手）；否则按关键帧（更快）
    public func seek(to seconds: Double, exact: Bool = true) {
        command(["seek", String(format: "%.3f", max(0, seconds)), exact ? "absolute+exact" : "absolute+keyframes"])
    }

    public func setSpeed(_ speed: Double) { setDouble("speed", speed) }

    /// 执行任意 mpv 命令（参数数组，无需结尾 nil）
    @discardableResult
    public func command(_ args: [String]) -> Int32 {
        handle.command(args)
    }

    // MARK: - 属性

    public func setString(_ name: String, _ value: String) {
        mpv_set_property_string(handle.mpv, name, value)
    }

    public func setFlag(_ name: String, _ value: Bool) {
        var flag: Int32 = value ? 1 : 0
        mpv_set_property(handle.mpv, name, MPV_FORMAT_FLAG, &flag)
    }

    public func setDouble(_ name: String, _ value: Double) {
        var number = value
        mpv_set_property(handle.mpv, name, MPV_FORMAT_DOUBLE, &number)
    }

    public func double(_ name: String) -> Double? {
        var value = 0.0
        return mpv_get_property(handle.mpv, name, MPV_FORMAT_DOUBLE, &value) >= 0 ? value : nil
    }

    public func int(_ name: String) -> Int? {
        var value: Int64 = 0
        return mpv_get_property(handle.mpv, name, MPV_FORMAT_INT64, &value) >= 0 ? Int(value) : nil
    }

    public func flag(_ name: String) -> Bool? {
        var value: Int32 = 0
        return mpv_get_property(handle.mpv, name, MPV_FORMAT_FLAG, &value) >= 0 ? value != 0 : nil
    }

    public func string(_ name: String) -> String? {
        guard let raw = mpv_get_property_string(handle.mpv, name) else { return nil }
        defer { mpv_free(raw) }
        return String(cString: raw)
    }

    // MARK: - 后台采样的读数

    /// 最近一次后台采样到的读数（诊断面板、看门狗用；最多晚一秒）
    public private(set) var readouts = MPVReadouts()
    private var sampling = false

    /// 在后台队列上读一批属性，读完回主线程更新 `readouts`。
    ///
    /// 为什么不在主线程直接读：`mpv_get_property` 要拿 mpv 的核心锁。核心线程卡在打开音频输出
    /// （AURemoteIO 启动）、换流、网络 I/O 时，主线程每秒一次的同步读数会跟着一起卡住——
    /// 界面点什么都没反应，严重时系统判定超时直接杀掉进程（第二轮审计 R-9 崩溃现场：
    /// 主线程卡在 `mpv_get_property ← MPVEngine.stats()`，核心线程卡在 `ao_start`）。
    /// 上一轮还没读完就跳过这一轮，不堆积。
    public func sampleReadouts(doubles: [String], ints: [String], strings: [String]) {
        guard !sampling else { return }
        sampling = true
        handle.sample(doubles: doubles, ints: ints, strings: strings) { [weak self] result in
            guard let self else { return }
            self.sampling = false
            if let result { self.readouts = result }
        }
    }

    /// 当前文件的全部轨道（音频/视频/字幕，含 sub-add 挂上的外挂轨）
    public func tracks() -> [MPVTrack] {
        let count = int("track-list/count") ?? 0
        return (0 ..< count).compactMap { index in
            let prefix = "track-list/\(index)"
            guard let id = int("\(prefix)/id"), let type = string("\(prefix)/type") else { return nil }
            return MPVTrack(
                id: id,
                type: type,
                language: string("\(prefix)/lang"),
                title: string("\(prefix)/title"),
                codec: string("\(prefix)/codec"),
                isExternal: flag("\(prefix)/external") ?? false,
                isSelected: flag("\(prefix)/selected") ?? false,
                channels: int("\(prefix)/demux-channel-count")
            )
        }
    }

    /// 前后台切换：进后台时关掉视频输出（只留音频，避免回前台黑屏——MPVKit 示例同款做法）
    public func setVideoOutputEnabled(_ enabled: Bool) {
        setString("vid", enabled ? "auto" : "no")
    }

    // MARK: - 销毁

    /// 释放 libmpv。调用后对象不可再用；渲染视图会在 mpv 线程全部退出后才释放。
    public func destroy() {
        onEvent = nil
        handle.sink = nil
        fallbackTask?.cancel()
        rebuildTask?.cancel()
        if let glView = view as? MPVGLView {
            // GL 渲染上下文必须在 GL 上下文当前、且 mpv 还活着时释放
            glView.detach()
        }
        let keepAlive = UncheckedBox(view)
        handle.destroy {
            // 在 mpv 线程全部退出后再放掉渲染视图（Metal 层被 vo 引用着）
            _ = keepAlive
        }
    }

    // MARK: - 版本与许可

    /// libmpv / FFmpeg 版本号（诊断面板的许可说明用）；首次读取时临时起一个空句柄
    public static let versionInfo: (mpv: String, ffmpeg: String) = readVersions()

    private static func readVersions() -> (mpv: String, ffmpeg: String) {
        guard let mpv = mpv_create() else { return ("未知", "未知") }
        defer { mpv_terminate_destroy(mpv) }
        mpv_initialize(mpv)
        func read(_ name: String) -> String {
            guard let raw = mpv_get_property_string(mpv, name) else { return "未知" }
            defer { mpv_free(raw) }
            return String(cString: raw)
        }
        return (read("mpv-version"), read("ffmpeg-version"))
    }

    /// 许可说明（LGPL 合规：告知使用了哪些 LGPL 组件、如何获得源码、如何替换）
    public static let licenseNotice = """
    本 App 的 MPV 播放引擎使用 libmpv 与 FFmpeg（均按 LGPL 2.1 或更高版本授权，\
    未启用任何 GPL 组件），以独立的动态框架 MPVCore.framework 形式链接，可被替换为自行编译的版本。\
    源码：https://github.com/mpv-player/mpv 、https://ffmpeg.org ，\
    构建脚本：https://github.com/mpvkit/MPVKit 。
    """
}

// MARK: - 公开类型

nonisolated public enum MPVRenderBackend: String, Sendable {
    /// Metal：MoltenVK + gpu-next（真机默认，支持 10bit 与 HDR）
    case metal
    /// OpenGL ES 渲染 API（备用路径；不支持 10bit 正确显示）
    case openGL

    nonisolated static var isSimulator: Bool {
        #if targetEnvironment(simulator)
        true
        #else
        false
        #endif
    }

    /// 真机默认 Metal。模拟器默认 OpenGL ES：实测 iOS 27 模拟器上 MoltenVK 能放 1080p 8bit，
    /// 但 4K 10bit（HEVC/杜比视界）上传纹理时模拟器的 Metal 驱动（MTLSimDriver）申请共享内存越界直接崩溃，
    /// 这是模拟器驱动的限制，真机不受影响
    nonisolated public static var automatic: MPVRenderBackend {
        #if DEBUG
        // 开发期启动参数 `-mcMPVBackend metal|openGL`：在模拟器上强制走真机的 Metal 路径复现问题
        if let raw = UserDefaults.standard.string(forKey: "mcMPVBackend"), let forced = MPVRenderBackend(rawValue: raw) {
            return forced
        }
        #endif
        return isSimulator ? .openGL : .metal
    }
}

/// 一次后台采样的属性读数（读不到的属性不在字典里）
nonisolated public struct MPVReadouts: Sendable {
    public var doubles: [String: Double] = [:]
    public var ints: [String: Int] = [:]
    public var strings: [String: String] = [:]
    public init() {}
}

nonisolated public struct MPVTrack: Sendable, Hashable {
    /// mpv 的轨 id（同类型内从 1 开始），用于 aid / sid
    public let id: Int
    /// video / audio / sub
    public let type: String
    public let language: String?
    public let title: String?
    public let codec: String?
    public let isExternal: Bool
    public let isSelected: Bool
    public let channels: Int?
}

nonisolated public enum MPVValue: Sendable, Equatable {
    case double(Double)
    case flag(Bool)
    case int(Int64)
    case none
}

nonisolated public enum MPVEvent: Sendable {
    case fileLoaded
    /// seek 完成 / 起播完成（可以出画了）
    case playbackRestart
    /// 文件结束。reason：eof / stop / quit / error / redirect；error 时附 mpv 的错误描述
    case endFile(reason: String, error: String?)
    case property(String, MPVValue)
    case log(String)
}

nonisolated public enum MPVError: LocalizedError {
    case createFailed
    case initializeFailed(String)
    case renderInitFailed(String)

    public var errorDescription: String? {
        switch self {
        case .createFailed: "MPV 播放引擎创建失败"
        case let .initializeFailed(reason): "MPV 播放引擎初始化失败：\(reason)"
        case let .renderInitFailed(reason): "MPV 渲染初始化失败：\(reason)"
        }
    }
}

/// 把非 Sendable 的引用带过线程边界（只用来延长生命周期，不在别的线程上访问）
nonisolated struct UncheckedBox<T>: @unchecked Sendable {
    let value: T
    init(_ value: T) { self.value = value }
}

// MARK: - 句柄与事件循环（非主线程）

/// mpv 句柄的线程安全包装。事件循环跑在私有串行队列上。
nonisolated final class MPVHandle: @unchecked Sendable {
    let mpv: OpaquePointer
    private let queue = DispatchQueue(label: "movieclaw.mpv.events", qos: .userInitiated)
    /// 读数采样队列：可能被 mpv 核心锁卡住，所以和事件队列分开，卡住也不耽误事件派发
    private let sampleQueue = DispatchQueue(label: "movieclaw.mpv.sample", qos: .utility)
    private let lock = NSLock()
    private var _sink: (@MainActor (MPVEvent) -> Void)?
    private var destroyed = false

    var sink: (@MainActor (MPVEvent) -> Void)? {
        get { lock.withLock { _sink } }
        set { lock.withLock { _sink = newValue } }
    }

    init(mpv: OpaquePointer) {
        self.mpv = mpv
    }

    static func log(_ message: String) {
        #if DEBUG
        print("[MPVCore] \(message)")
        #endif
    }

    func startEventLoop() {
        let context = Unmanaged.passUnretained(self).toOpaque()
        mpv_set_wakeup_callback(mpv, { raw in
            guard let raw else { return }
            let handle = Unmanaged<MPVHandle>.fromOpaque(raw).takeUnretainedValue()
            handle.queue.async { handle.drain() }
        }, context)
    }

    func command(_ args: [String]) -> Int32 {
        var cArgs: [UnsafePointer<CChar>?] = args.map { UnsafePointer(strdup($0)) }
        cArgs.append(nil)
        defer { for pointer in cArgs where pointer != nil { free(UnsafeMutablePointer(mutating: pointer)) } }
        let status = mpv_command(mpv, &cArgs)
        if status < 0 {
            Self.log("命令失败 \(args.first ?? "")：\(String(cString: mpv_error_string(status)))")
        }
        return status
    }

    /// 在采样队列上读属性，完成后回主线程交给 completion（已销毁则给 nil）
    func sample(doubles: [String], ints: [String], strings: [String], completion: @escaping @MainActor @Sendable (MPVReadouts?) -> Void) {
        sampleQueue.async { [self] in
            var result: MPVReadouts?
            if !lock.withLock({ destroyed }) {
                var readouts = MPVReadouts()
                for name in doubles {
                    var value = 0.0
                    if mpv_get_property(mpv, name, MPV_FORMAT_DOUBLE, &value) >= 0 { readouts.doubles[name] = value }
                }
                for name in ints {
                    var value: Int64 = 0
                    if mpv_get_property(mpv, name, MPV_FORMAT_INT64, &value) >= 0 { readouts.ints[name] = Int(value) }
                }
                for name in strings {
                    if let raw = mpv_get_property_string(mpv, name) {
                        readouts.strings[name] = String(cString: raw)
                        mpv_free(raw)
                    }
                }
                result = readouts
            }
            let final = result
            DispatchQueue.main.async { MainActor.assumeIsolated { completion(final) } }
        }
    }

    /// 在事件队列上排空全部待处理事件
    private func drain() {
        while true {
            if lock.withLock({ destroyed }) { return }
            guard let event = mpv_wait_event(mpv, 0)?.pointee else { return }
            if event.event_id == MPV_EVENT_NONE { return }
            guard let parsed = parse(event) else { continue }
            let sink = self.sink
            if let sink {
                DispatchQueue.main.async { MainActor.assumeIsolated { sink(parsed) } }
            }
        }
    }

    private func parse(_ event: mpv_event) -> MPVEvent? {
        switch event.event_id {
        case MPV_EVENT_FILE_LOADED:
            return .fileLoaded
        case MPV_EVENT_PLAYBACK_RESTART:
            return .playbackRestart
        case MPV_EVENT_END_FILE:
            guard let data = event.data?.assumingMemoryBound(to: mpv_event_end_file.self).pointee else {
                return .endFile(reason: "unknown", error: nil)
            }
            let reason: String = switch data.reason {
            case MPV_END_FILE_REASON_EOF: "eof"
            case MPV_END_FILE_REASON_STOP: "stop"
            case MPV_END_FILE_REASON_QUIT: "quit"
            case MPV_END_FILE_REASON_ERROR: "error"
            case MPV_END_FILE_REASON_REDIRECT: "redirect"
            default: "unknown"
            }
            let error = data.reason == MPV_END_FILE_REASON_ERROR ? String(cString: mpv_error_string(data.error)) : nil
            return .endFile(reason: reason, error: error)
        case MPV_EVENT_PROPERTY_CHANGE:
            guard let property = event.data?.assumingMemoryBound(to: mpv_event_property.self).pointee else { return nil }
            let name = String(cString: property.name)
            let value: MPVValue
            switch property.format {
            case MPV_FORMAT_DOUBLE:
                value = property.data.map { .double($0.assumingMemoryBound(to: Double.self).pointee) } ?? .none
            case MPV_FORMAT_FLAG:
                value = property.data.map { .flag($0.assumingMemoryBound(to: Int32.self).pointee != 0) } ?? .none
            case MPV_FORMAT_INT64:
                value = property.data.map { .int($0.assumingMemoryBound(to: Int64.self).pointee) } ?? .none
            default:
                value = .none
            }
            return .property(name, value)
        case MPV_EVENT_LOG_MESSAGE:
            guard let message = event.data?.assumingMemoryBound(to: mpv_event_log_message.self).pointee else { return nil }
            let text = "[\(String(cString: message.prefix))] \(String(cString: message.level)): \(String(cString: message.text))"
            Self.log(text.trimmingCharacters(in: .newlines))
            return .log(text)
        default:
            return nil
        }
    }

    /// 摘掉回调并在事件队列上销毁 mpv；完成后在主线程调用 completion
    func destroy(completion: @escaping @Sendable () -> Void) {
        lock.withLock {
            destroyed = true
            _sink = nil
        }
        mpv_set_wakeup_callback(mpv, nil, nil)
        let mpv = UncheckedBox(self.mpv)
        let sampleQueue = self.sampleQueue
        queue.async {
            // 等进行中的采样读完再销毁：采样线程还拿着句柄
            sampleQueue.sync {}
            mpv_terminate_destroy(mpv.value)
            DispatchQueue.main.async(execute: completion)
        }
    }
}


#if DEBUG
/// 真机排查用：写 stderr，`xcrun devicectl device process launch --console` 能收到
nonisolated enum MPVDiag {
    static func log(_ message: String) {
        let line = Data("[MPVDiag] \(message)\n".utf8)
        FileHandle.standardError.write(line)
        #if targetEnvironment(simulator)
        // 模拟器：同时追加到宿主机文件，方便命令行读取
        let url = URL(fileURLWithPath: "/tmp/mc-mpvdiag.log")
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile(); handle.write(line); try? handle.close()
        } else {
            try? line.write(to: url)
        }
        #endif
    }
}
#endif
