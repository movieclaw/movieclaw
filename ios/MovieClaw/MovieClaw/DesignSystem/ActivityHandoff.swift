import SwiftUI

/// 模型接入探测（对应 Web `LlmCapabilityProvider`）：`GET /llm/providers` 非空即视为已接入。
///
/// 全 App 共享一个实例，多个「交给 AI 分析」入口不重复请求；结果缓存 5 分钟（设置里新接入模型后
/// 下次打开页面即生效）。探测失败按「不可用」处理并**放行**入口（fail-open），由后端预检兜底——
/// 与 Web 同口径：一次状态请求故障不应锁死所有 AI 功能。
@Observable
final class LLMCapabilityProbe {
    enum State { case checking, configured, missing, unavailable }

    static let shared = LLMCapabilityProbe()

    private(set) var state: State = .checking
    @ObservationIgnored private var checkedAt: Date?
    @ObservationIgnored private var inFlight = false

    /// 入口是否该显示：已接入，或探测失败（放行）
    var allowsHandoff: Bool { state == .configured || state == .unavailable }

    func ensure(api: APIClient) async {
        if let checkedAt, Date.now.timeIntervalSince(checkedAt) < 300 { return }
        guard !inFlight else { return }
        inFlight = true
        defer { inFlight = false }
        do {
            let providers = try await api.llmProvidersList()
            state = providers.isEmpty ? .missing : .configured
            checkedAt = .now
        } catch is CancellationError {
        } catch {
            state = .unavailable
        }
    }
}

nonisolated extension APIClient {
    /// 「交给 AI 分析」：后端按事项类型组装诊断工单（`POST /agent-handoff`），再用工单起一个新会话
    /// （`POST /sessions`），返回会话编号。kind：notice（告警 id）/ download（info_hash）/ job（任务 id）。
    func activityHandoff(kind: String, ref: String) async throws -> String {
        let prompt = try await sessionHandoffPrompt(body: API.HandoffRequest(kind: kind, ref: ref))
        let accepted = try await sessionStart(body: API.SessionStartPayload(content: prompt.prompt))
        return accepted.sessionId
    }
}

/// 「交给 AI 分析」按钮：每条待处理事项的第二出口（Web `HandoffButton`）。
///
/// 第一个按钮永远是事项确定性的出口（去处理 / 删除 / 换种）；这个按钮负责「不知道该怎么办」：
/// 工单由后端带着现场自检组装（前端不拼），起会话后跳到会话页。未接入模型时整个不渲染。
struct ActivityHandoffButton: View {
    let kind: String
    let refId: String
    /// 跳转前的收尾（关弹层等）
    var onBeforeNavigate: (() -> Void)?

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @Environment(Feedback.self) private var feedback
    @State private var busy = false
    private var probe: LLMCapabilityProbe { .shared }

    var body: some View {
        Group {
            if probe.allowsHandoff {
                Button {
                    Task { await handoff() }
                } label: {
                    Text(busy ? "正在整理上下文…" : "交给 AI 分析")
                        .font(.subheadline.weight(.medium))
                }
                .buttonStyle(.glass)
                .disabled(busy)
                .accessibilityIdentifier("handoff-\(kind)-\(refId)")
            }
        }
        .task { await probe.ensure(api: api) }
    }

    private func handoff() async {
        busy = true
        defer { busy = false }
        do {
            let sessionId = try await api.activityHandoff(kind: kind, ref: refId)
            onBeforeNavigate?()
            router.open(.session(id: sessionId))
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "无法发起 AI 分析" : error.localizedDescription)
        }
    }
}
