import SwiftUI

/// 可见期间轮询（对应 Web `useVisiblePolling`）：
/// - 页面出现时开始、离开时自动停止（绑定在 `.task` 生命周期上）；
/// - App 退到后台暂停，回到前台立即刷新一次再继续；
/// - 间隔可以随状态变化（例如「扫描中 3 秒、空闲 30 秒」），每轮重新取值。
///
/// ```swift
/// .polling(every: busy ? 3 : 30) { await reload() }
/// ```
struct PollingModifier: ViewModifier {
    let interval: () -> Duration
    let immediately: Bool
    let action: () async -> Void
    @Environment(\.scenePhase) private var scenePhase

    func body(content: Content) -> some View {
        content
            .task(id: scenePhase) {
                guard scenePhase == .active else { return }
                if immediately { await action() }
                while !Task.isCancelled {
                    try? await Task.sleep(for: interval())
                    if Task.isCancelled { break }
                    await action()
                }
            }
    }
}

extension View {
    /// - Parameters:
    ///   - seconds: 轮询间隔（秒），每轮重新求值
    ///   - immediately: 开始时（含回到前台）是否先立即执行一次；页面自己在 `.task` 里首载时传 false
    func polling(every seconds: @autoclosure @escaping () -> Double, immediately: Bool = false, _ action: @escaping () async -> Void) -> some View {
        modifier(PollingModifier(interval: { .seconds(seconds()) }, immediately: immediately, action: action))
    }
}
