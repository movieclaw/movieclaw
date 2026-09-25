import Foundation

#if DEBUG
/// 开发期启动参数（仅 Debug 构建生效，Release 里整段不存在）：
///
///     xcrun simctl launch <设备> com.movieclaw.app \
///         -mcServer http://localhost:3000 -mcUser admin -mcPass xxx -mcRoute /library/1
///
/// - `-mcServer/-mcUser/-mcPass`：跳过连接页与登录页，直接以该账号进入；
/// - `-mcRoute`：启动后打开一个 Web 站内路径（与网页地址一一对应），
///   用于和浏览器同一路由截图对照；`/play/{id}` 直接打开播放器。
///
/// 参数经 UserDefaults 的命令行域读取（`-key value` 形式）。
enum DebugLaunch {
    static var server: String? { UserDefaults.standard.string(forKey: "mcServer") }
    static var username: String? { UserDefaults.standard.string(forKey: "mcUser") }
    static var password: String? { UserDefaults.standard.string(forKey: "mcPass") }
    static var route: String? { UserDefaults.standard.string(forKey: "mcRoute") }
}
#endif
