import SwiftUI

/// 可见期间轮询（对应 Web `useVisiblePolling`）：
/// - 页面出现时开始、离开时自动停止（绑定在 `.task` 生命周期上）；
/// - App 退到后台暂停，回到前台立即补刷一次再继续（Web 的 visibilitychange 补刷，与 `immediately` 无关）；
/// - 间隔可以随状态变化（例如「扫描中 3 秒、空闲 30 秒」）：间隔一变就按新间隔**重新起表**
///   （Web 的 effect 依赖 intervalMs 重建定时器），不会先睡完上一轮的 30 秒才切到快轮询；
///   重新起表时 `immediately` 同样生效（Web `leading` 在 intervalMs 变化时也会先执行一次）。
///
/// ```swift
/// .polling(every: busy ? 3 : 30) { await reload() }
/// ```
struct PollingModifier: ViewModifier {
    let seconds: Double
    let immediately: Bool
    let action: () async -> Void
    @Environment(\.scenePhase) private var scenePhase
    /// 离开过前台：回来时要补刷一次
    @State private var missedWhileInactive = false

    /// 任务身份：前后台与间隔任一变化都重启轮询
    private struct PollingKey: Equatable {
        var active: Bool
        var seconds: Double
    }

    func body(content: Content) -> some View {
        content
            .task(id: PollingKey(active: scenePhase == .active, seconds: seconds)) {
                guard scenePhase == .active else {
                    missedWhileInactive = true
                    return
                }
                if immediately || missedWhileInactive {
                    missedWhileInactive = false
                    await action()
                }
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(seconds))
                    if Task.isCancelled { break }
                    await action()
                }
            }
    }
}

extension View {
    /// - Parameters:
    ///   - seconds: 轮询间隔（秒）；随页面状态变化时立即按新间隔重新起表
    ///   - immediately: 开始时（含间隔变化后重新起表）是否先立即执行一次；页面自己在 `.task` 里首载时传 false。
    ///     回到前台的补刷不受它影响，总会执行
    func polling(every seconds: Double, immediately: Bool = false, _ action: @escaping () async -> Void) -> some View {
        modifier(PollingModifier(seconds: seconds, immediately: immediately, action: action))
    }
}
