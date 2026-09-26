import Darwin
import Foundation
import ServiceManagement

/// 开机自启动 + App 崩溃后自动拉起：`~/Library/LaunchAgents/com.movieclaw.transcoder.plist`。
///
/// ## 为什么不用 SMAppService.agent
///
/// 那是 Apple 推荐的新写法，但它把登录项和注册那一刻 App 的 cdhash 钉死（launchd 里的
/// LWCR）。我们的 App 是 ad-hoc 签名，每次构建 cdhash 都变：更新之后 launchd 以 EX_CONFIG
/// 拒绝拉起新版本（还每 10 秒重试一次），开机不再自启；重新注册也得等系统在后台处理完新
/// 版本（实测要半分钟以上）才管用。传统的 LaunchAgent 配置没有这层约束，换了版本照样拉起
/// （都是 macOS 27 实测）。等 App 有了 Developer ID 签名（约束按开发者而不是 cdhash），
/// 可以换回 SMAppService。
///
/// ## 行为
///
/// - RunAtLoad：登录后自动启动；KeepAlive.SuccessfulExit=false：意外退出（崩溃、被强杀）后
///   launchd 重新拉起，用户点「退出」（退出码 0）不拉起；ThrottleInterval 防崩溃循环刷屏。
///   转码内核的崩溃不靠它——那是 ``CoreSupervisor`` 在 App 里就兜住的。
/// - 配置里写的是 App 可执行文件的绝对路径：App 挪了位置，下次打开时改写（``applyOnLaunch()``）。
/// - 「系统设置 → 通用 → 登录项」里能看到它（AssociatedBundleIdentifiers 让它挂在 App 名下）；
///   用户在那边关掉后，这里读到的是「需要允许」。
/// - 只有 launchd 拉起的进程才受 KeepAlive 保护。手动打开（更新后重新打开之类）时会请
///   launchd 另起一个、自己退出交班（见 AppMain），不然一台常年不关机的 Mac 更新一次之后
///   就一直没有这层保护。
///
/// 默认打开：转码器是给 NAS 干活的常驻服务，开机不起来就等于这台 Mac 不在线。用户的选择
/// 记在 UserDefaults 里（没选过算「要」），每次启动对一遍。
@MainActor
enum LoginItem {
    /// launchd 里的名字，也是配置文件名。和旧版 README 教用户手动装的那份同名：打开开关时
    /// 直接替换接管，不会出现两份。launchd 给自己拉起的进程设的 XPC_SERVICE_NAME 就是它；
    /// 手动打开的进程是 `application.com.movieclaw.transcoder.…`。
    static let label = "com.movieclaw.transcoder"
    /// 用户要不要开机自启动。只有在我们自己的设置里关掉才会是 false。
    private static let wantedKey = "movieclaw.launchAtLogin"

    enum State: Equatable {
        case enabled
        case disabled
        /// 配置装好了，但用户在「系统设置 → 登录项」里把它关了，要去那边允许。
        case requiresApproval
    }

    /// 这个进程是 launchd 按登录项拉起的（意外退出后会被重新拉起）。
    static var isLaunchdInstance: Bool {
        ProcessInfo.processInfo.environment["XPC_SERVICE_NAME"] == label
    }

    static var state: State {
        guard installedProgram == currentProgram else { return .disabled }
        if #available(macOS 13.0, *), SMAppService.statusForLegacyPlist(at: plistURL) == .requiresApproval {
            return .requiresApproval
        }
        return .enabled
    }

    static func setEnabled(_ enabled: Bool) throws {
        UserDefaults.standard.set(enabled, forKey: wantedKey)
        if enabled {
            try install()
            AppLogger.shared.info("已打开开机自启动")
        } else {
            uninstall()
            AppLogger.shared.info("已关闭开机自启动")
        }
    }

    /// 每次启动时对一遍：用户要开机自启动，而配置没装、装的是别处的 App（挪了位置、旧版手动
    /// 装的），或者没装载进 launchd，就装好。用户在系统设置里关掉的（「需要允许」）不动。
    static func applyOnLaunch() {
        guard UserDefaults.standard.object(forKey: wantedKey) as? Bool ?? true,
              state != .requiresApproval,
              installedProgram != currentProgram || !isLoaded
        else { return }
        do {
            try setEnabled(true)
        } catch {
            // 写不了配置（权限、磁盘满）不影响正常使用
            AppLogger.shared.warning("打开开机自启动失败：\(error.localizedDescription)")
        }
    }

    /// 请 launchd 按登录项立刻拉起一个实例（手动打开时交班用）。已经在跑就什么也不发生，
    /// 所以不看结果：调用方只认「另一个实例出现了没有」。
    static func kickstart() {
        launchctl("kickstart", "\(domain)/\(label)")
    }

    static func openSystemSettings() {
        if #available(macOS 13.0, *) {
            SMAppService.openSystemSettingsLoginItems()
        }
    }

    // MARK: - 配置文件与 launchd

    private static var plistURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }

    private static var domain: String { "gui/\(getuid())" }

    private static var currentProgram: String? { Bundle.main.executablePath }

    /// 已装配置里写的可执行文件路径；没装为 nil。
    private static var installedProgram: String? {
        guard let data = try? Data(contentsOf: plistURL),
              let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              let arguments = plist["ProgramArguments"] as? [String]
        else { return nil }
        return arguments.first
    }

    private static var isLoaded: Bool {
        launchctl("print", "\(domain)/\(label)") == 0
    }

    private static func install() throws {
        guard let program = currentProgram else {
            throw ConfigurationError.message("找不到 App 的可执行文件路径")
        }
        let plist: [String: Any] = [
            "Label": label,
            "ProgramArguments": [program],
            "AssociatedBundleIdentifiers": [Bundle.main.bundleIdentifier ?? "com.movieclaw.transcoder"],
            "RunAtLoad": true,
            "KeepAlive": ["SuccessfulExit": false],
            "ThrottleInterval": 10,
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",
        ]
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        try FileManager.default.createDirectory(
            at: plistURL.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try data.write(to: plistURL, options: .atomic)
        // launchd 管着的就是自己：配置已装载，重新装载会先把自己结束掉，写好文件就够了
        guard !isLaunchdInstance else { return }
        // 换下已装载的旧配置（旧位置、旧版手动装的）再按新配置装载。手动打开的实例不会和
        // launchd 管着的实例同时在跑（防多开），所以这里结束不了任何在干活的进程。装载时按
        // RunAtLoad 就会拉起一个实例，手动打开的这个随后交班给它（见 AppMain）
        launchctl("bootout", "\(domain)/\(label)")
        launchctl("bootstrap", domain, plistURL.path)
    }

    private static func uninstall() {
        try? FileManager.default.removeItem(at: plistURL)
        // launchd 管着的就是自己时不卸载（卸载会把自己结束掉）：配置删了，下次登录就不会再
        // 拉起，这一轮点「退出」本来也不会被拉起
        if !isLaunchdInstance {
            launchctl("bootout", "\(domain)/\(label)")
        }
    }

    @discardableResult
    private static func launchctl(_ arguments: String...) -> Int32 {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = arguments
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
        } catch {
            AppLogger.shared.warning("无法执行 launchctl \(arguments.first ?? "")：\(error.localizedDescription)")
            return -1
        }
        task.waitUntilExit()
        return task.terminationStatus
    }
}
