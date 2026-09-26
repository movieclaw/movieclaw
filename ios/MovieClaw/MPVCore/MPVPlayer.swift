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

    private let handle: MPVHandle
    /// Metal 渲染表面（OpenGL 路径为 nil）：尺寸变化时要通知 mpv 重新排布画面
    private var metalViewForResize: MPVMetalView?
    private var resizeTask: Task<Void, Never>?

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

        #if DEBUG
        mpv_request_log_messages(mpv, "warn")
        #else
        mpv_request_log_messages(mpv, "error")
        #endif

        switch chosen {
        case .metal:
            let metalView = MPVMetalView()
            view = metalView
            metalViewForResize = metalView
            // wid 传的是 CAMetalLayer 的指针（MPVKit 的 moltenvk 补丁约定）
            var layerPointer = Int64(Int(bitPattern: Unmanaged.passUnretained(metalView.metalLayer).toOpaque()))
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
            self?.onEvent?(event)
        }
        handle.startEventLoop()

        // 视频输出尺寸与视图对齐：MPVKit 的 moltenvk 上下文只在 VO 初始化/视频配置时读一次
        // drawableSize（ra_vk_ctx_resize），之后的尺寸变化它感知不到。两种后果（真机与模拟器 Metal 路径实测）：
        // ① 转横屏后仍按竖屏尺寸（1206x2622）排布画面——压扁、偏到一边；
        // ② 起播时 VO 先于界面排版初始化，拿到 0 尺寸、输出停在 1x1——整片黑屏。
        // 所以视图尺寸变化后、以及起播后各核对一次，对不上就重建输出（见 scheduleVideoRelayout）。
        metalViewForResize?.onDrawableSizeChange = { [weak self] _ in
            self?.scheduleVideoRelayout()
        }
    }

    /// 等尺寸稳定（旋转动画结束）后核对 mpv 输出尺寸，对不上就关开一次视频轨重建 VO。
    ///
    /// 为什么用关开视频轨：iOS 版 libmpv 没有 android-surface-size 之类的外部尺寸通知（设置返回 -12），
    /// video-reload 在参数不变时跳过配置、切换 video-aspect-override 也不重算输出尺寸（逐一实测过）。
    /// 关开视频轨会销毁并重建 VO，重建时读到的就是当前尺寸——与进出后台的 setVideoOutputEnabled 同一机制，
    /// 代价是瞬间黑一下，只在尺寸真的对不上时才做。
    private func scheduleVideoRelayout(after delay: Duration = .milliseconds(250)) {
        guard metalViewForResize != nil else { return }
        resizeTask?.cancel()
        resizeTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled, let self, let layer = self.metalViewForResize?.metalLayer else { return }
            // 没有在放视频（未加载、已关视频输出、VO 还没初始化）时不用管，初始化时自然读到当前尺寸
            guard let vid = self.string("vid"), vid != "no", self.string("path") != nil else { return }
            let target = layer.drawableSize
            let width = self.int("osd-width") ?? 0, height = self.int("osd-height") ?? 0
            guard width > 0, height > 0, width != Int(target.width) || height != Int(target.height) else { return }
            self.setString("vid", "no")
            try? await Task.sleep(for: .milliseconds(60))
            guard !Task.isCancelled else { return }
            self.setString("vid", vid)
        }
    }

    // MARK: - 播放控制

    /// 打开一个地址。`start` 是起播秒数（mpv 的 start 选项，对下一个文件生效）。
    public func load(_ url: URL, start: Double?, paused: Bool) {
        setString("start", start.map { String(format: "%.3f", max(0, $0)) } ?? "none")
        setFlag("pause", paused)
        command(["loadfile", url.absoluteString, "replace"])
        // 起播后核对一次输出尺寸（VO 可能先于界面排版初始化，停在 1x1）
        scheduleVideoRelayout(after: .seconds(2))
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
        queue.async {
            mpv_terminate_destroy(mpv.value)
            DispatchQueue.main.async(execute: completion)
        }
    }
}

