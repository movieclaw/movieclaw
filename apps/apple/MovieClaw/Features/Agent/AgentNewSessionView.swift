import SwiftUI

/// 新任务页（`/new`，对应 Web `components/new-task.tsx` 的银玻璃手机形态）。
///
/// 一张「还没有消息的会话页」：顶栏「新会话」+ 返回、正文空着、输入框钉在底部，与会话页完全同构。
/// 发出第一条消息 = 创建会话，等服务端分配 session_id 后**替换**当前路由到 `/sessions/{id}`
/// （不叠两层：返回键直接回到进入新任务前的页面，与 Web `router.replace` 一致）。
///
/// 新会话没有可沿用的历史：模型/思维链以本机记住的上次选择为起点，用户一改就记下、并显式随消息提交。
/// 未接入模型时锁定输入框并引导去设置。
struct AgentNewSessionView: View {
    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    @State private var draft = AgentDraft()
    @State private var choice = AgentComposerPrefs.load()
    @State private var modelOptions: [API.LlmModelOptionView] = []
    @State private var llmConfigured: Bool?
    @State private var creating = false
    @State private var error: String?

    var body: some View {
        let locked = llmConfigured == false
        Color.clear
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(.rect)
            .safeAreaInset(edge: .bottom, spacing: 0) {
                VStack(alignment: .leading, spacing: 10) {
                    AgentComposer(
                        draft: $draft,
                        placeholder: locked ? "请先接入 AI 模型，再开始对话" : nil,
                        busy: creating,
                        disabled: locked,
                        autoFocus: !locked,
                        modelOptions: modelOptions,
                        modelValue: choice.model,
                        // 换模型后旧档位可能不在新菜单里，清回默认
                        onModelChange: { update(AgentComposerPrefs(model: $0, thinking: nil)) },
                        thinkingValue: choice.thinking,
                        onThinkingChange: { update(AgentComposerPrefs(model: choice.model, thinking: $0)) },
                        onSubmit: submit
                    )
                    if locked { AgentLLMSetupNotice() }
                    if let error {
                        Text("创建会话失败：\(error)")
                            .font(.system(size: 14))
                            .foregroundStyle(Theme.danger)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 14).padding(.vertical, 10)
                            .background(Theme.surfaceRaised, in: .rect(cornerRadius: 12))
                            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.danger.opacity(0.35)))
                            .accessibilityIdentifier("agent-create-error")
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            }
            .background(Theme.background.ignoresSafeArea())
            .navigationTitle("新会话")
            .navigationBarTitleDisplayMode(.inline)
            .hidesTabBar()
            .toolbar { AgentTopBarActions() }
            .task {
                async let configured = AgentCatalog.llmConfigured(api: api)
                async let options = AgentCatalog.modelOptions(api: api)
                llmConfigured = await configured
                modelOptions = await options
                // 清单回来后校验记忆：模型被删了 / 档位不在菜单里的记忆直接丢弃
                choice = choice.reconciled(with: modelOptions)
            }
    }

    private func update(_ next: AgentComposerPrefs) {
        choice = next
        next.save()
    }

    private func submit() {
        let message = draft.message
        let images = draft.images.map { AgentTurnImage(attachmentId: $0.attachmentId, name: $0.name) }
        creating = true
        error = nil
        Task {
            do {
                let id = try await AgentConversationRegistry.start(
                    api: api, input: message, images: images,
                    thinking: choice.thinking, model: choice.model
                )
                replaceRoute(with: id)
            } catch {
                self.error = error.localizedDescription
                creating = false
            }
        }
    }

    /// 这张空页是会话的「前一帧」，不该留在导航栈里：把栈顶的 /new 换成 /sessions/{id}
    private func replaceRoute(with id: String) {
        let tab = router.selectedTab
        if var path = router.paths[tab], path.last == .newSession {
            path[path.count - 1] = .session(id: id)
            router.paths[tab] = path
        } else {
            router.push(.session(id: id))
        }
    }
}
