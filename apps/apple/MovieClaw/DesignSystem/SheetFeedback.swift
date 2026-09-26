import SwiftUI

/// sheet 内的反馈宿主。
///
/// 全局 `FeedbackHost` 挂在标签栏根部：sheet 盖在上面时，根部的 `.alert` 无法弹出
/// （UIKit 不允许在已呈现 sheet 的控制器上再呈现），`feedback.confirm` 会永远等不到结果。
/// 所以每个 sheet 的内容换上一个自己的 `Feedback` 并就地挂宿主：确认框、输入框、
/// 错误提示都在 sheet 里显示；sheet 关闭时还没消失的轻提示转交回根部继续显示
/// （例如「已加入合集」后立刻关窗，提示不该跟着一起消失）。
struct SheetFeedback: ViewModifier {
    @Environment(Feedback.self) private var parent
    @State private var local = Feedback()

    func body(content: Content) -> some View {
        content
            .environment(local)
            .modifier(FeedbackHost(feedback: local))
            .onDisappear {
                for toast in local.toasts { parent.show(toast.tone, toast.message) }
            }
    }
}

extension View {
    /// 所有 sheet / fullScreenCover 的内容都要调用它（全 App 约定），见 `SheetFeedback`
    func sheetFeedback() -> some View { modifier(SheetFeedback()) }
}
