import SwiftUI

/// 全局反馈中心：轻提示（Toast）、确认框、输入框。对应 Web `components/feedback.tsx`。
///
/// 用法与 Web 端一致，方便逐行移植：
/// ```swift
/// @Environment(Feedback.self) private var feedback
/// feedback.success("已加入合集")
/// if await feedback.confirm("删除这个合集？", message: "…", destructive: true) { … }
/// if let name = await feedback.prompt("重命名", initial: old) { … }
/// ```
/// 确认/输入框用 async 等结果，调用方不用在每个页面各自挂 `.alert` 状态。
@Observable
final class Feedback {
    enum Tone { case success, info, error }

    struct Toast: Identifiable, Equatable {
        let id = UUID()
        var tone: Tone
        var message: String
        var actionTitle: String?
        var action: (@MainActor () -> Void)?

        static func == (lhs: Toast, rhs: Toast) -> Bool { lhs.id == rhs.id }
    }

    struct Confirm: Identifiable {
        let id = UUID()
        var title: String
        var message: String?
        var confirmTitle: String
        /// 取消键文案（网页部分确认框写「先不」）
        var cancelTitle: String = "取消"
        var destructive: Bool
        var resume: (Bool) -> Void
    }

    struct Prompt: Identifiable {
        let id = UUID()
        var title: String
        var message: String?
        var placeholder: String
        var text: String
        var confirmTitle: String
        /// 输入上限（字符数，同网页 prompt 的 maxLength）：超出时「确定」置灰，弹窗里写明上限
        var maxLength: Int?
        var resume: (String?) -> Void
    }

    private(set) var toasts: [Toast] = []
    var confirmRequest: Confirm?
    var promptRequest: Prompt?

    func success(_ message: String) { show(.success, message) }
    func info(_ message: String) { show(.info, message) }
    func error(_ message: String) { show(.error, message) }
    /// 直接展示错误对象（后端中文文案原样透出）
    func error(_ error: Error) {
        if error is CancellationError { return }
        show(.error, error.localizedDescription)
    }

    func show(_ tone: Tone, _ message: String, actionTitle: String? = nil, action: (@MainActor () -> Void)? = nil) {
        let toast = Toast(tone: tone, message: message, actionTitle: actionTitle, action: action)
        toasts.append(toast)
        // 最多同时 4 条（同 Web）
        if toasts.count > 4 { toasts.removeFirst(toasts.count - 4) }
        let seconds: Double = tone == .error ? 6 : 4
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(seconds))
            self?.dismiss(toast)
        }
    }

    func dismiss(_ toast: Toast) {
        toasts.removeAll { $0.id == toast.id }
    }

    func confirm(_ title: String, message: String? = nil, confirmTitle: String = "确定", cancelTitle: String = "取消", destructive: Bool = false) async -> Bool {
        await withCheckedContinuation { continuation in
            confirmRequest = Confirm(
                title: title, message: message, confirmTitle: confirmTitle, cancelTitle: cancelTitle, destructive: destructive,
                resume: { continuation.resume(returning: $0) }
            )
        }
    }

    func prompt(_ title: String, message: String? = nil, placeholder: String = "", initial: String = "", confirmTitle: String = "确定", maxLength: Int? = nil) async -> String? {
        await withCheckedContinuation { continuation in
            promptRequest = Prompt(
                title: title, message: message, placeholder: placeholder,
                text: maxLength.map { String(initial.prefix($0)) } ?? initial, confirmTitle: confirmTitle, maxLength: maxLength,
                resume: { continuation.resume(returning: $0) }
            )
        }
    }
}

/// 挂在根视图上，承载 Toast 与全局确认/输入框
struct FeedbackHost: ViewModifier {
    @Bindable var feedback: Feedback

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .top) {
                VStack(spacing: 8) {
                    ForEach(feedback.toasts) { toast in
                        ToastView(toast: toast) { feedback.dismiss(toast) }
                            .transition(.move(edge: .top).combined(with: .opacity))
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 4)
                .animation(.spring(duration: 0.3), value: feedback.toasts)
            }
            .alert(
                feedback.confirmRequest?.title ?? "",
                isPresented: Binding(
                    get: { feedback.confirmRequest != nil },
                    set: { if !$0, let request = feedback.confirmRequest { feedback.confirmRequest = nil; request.resume(false) } }
                ),
                presenting: feedback.confirmRequest
            ) { request in
                Button(request.cancelTitle, role: .cancel) { feedback.confirmRequest = nil; request.resume(false) }
                Button(request.confirmTitle, role: request.destructive ? .destructive : nil) {
                    feedback.confirmRequest = nil
                    request.resume(true)
                }
            } message: { request in
                if let message = request.message { Text(message) }
            }
            .alert(
                feedback.promptRequest?.title ?? "",
                isPresented: Binding(
                    get: { feedback.promptRequest != nil },
                    set: { if !$0, let request = feedback.promptRequest { feedback.promptRequest = nil; request.resume(nil) } }
                ),
                presenting: feedback.promptRequest
            ) { request in
                TextField(request.placeholder, text: Binding(
                    get: { feedback.promptRequest?.text ?? "" },
                    // 不在这里截断：系统弹窗的输入框不回显截断后的值，用户看着打满了、提交的却是前 N 个字
                    // （第三轮复核 S-13）。超长改为「确定」置灰，说明写在弹窗里
                    set: { feedback.promptRequest?.text = $0 }
                ))
                Button("取消", role: .cancel) { feedback.promptRequest = nil; request.resume(nil) }
                Button(request.confirmTitle) {
                    let text = feedback.promptRequest?.text ?? request.text
                    feedback.promptRequest = nil
                    request.resume(text)
                }
                .disabled(request.maxLength.map { (feedback.promptRequest?.text.count ?? 0) > $0 } ?? false)
            } message: { request in
                let limit = request.maxLength.map { "最多 \($0) 字，超出时无法确定。" }
                let lines = [request.message, limit].compactMap { $0 }
                if !lines.isEmpty { Text(lines.joined(separator: "\n")) }
            }
    }
}

private struct ToastView: View {
    let toast: Feedback.Toast
    let onDismiss: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon).foregroundStyle(color)
            Text(toast.message)
                .font(.subheadline)
                .foregroundStyle(Theme.text)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let title = toast.actionTitle, let action = toast.action {
                Button(title) { action(); onDismiss() }
                    .font(.subheadline.weight(.semibold))
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .glassEffect(.regular, in: .rect(cornerRadius: 18))
        .onTapGesture(perform: onDismiss)
        .accessibilityIdentifier("toast")
    }

    private var icon: String {
        switch toast.tone {
        case .success: "checkmark.circle.fill"
        case .info: "info.circle.fill"
        case .error: "exclamationmark.triangle.fill"
        }
    }

    private var color: Color {
        switch toast.tone {
        case .success: Theme.success
        case .info: Theme.info
        case .error: Theme.danger
        }
    }
}
