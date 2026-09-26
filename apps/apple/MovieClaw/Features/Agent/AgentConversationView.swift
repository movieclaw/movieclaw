import SwiftUI

/// AI 会话页（`/sessions/{id}`，对应 Web `components/agent-conversation-view.tsx`）。
///
/// ChatGPT / Claude 式对话：顶栏会话标题 + 返回 + 右上角「⋯」会话菜单，可滚动消息列（用户右侧气泡 / Agent 整栏正文），
/// 底部贴底输入框随键盘上移。沉浸式：进入时隐藏标签栏（Web 手机端 `/sessions/*` 同样隐藏底栏）。
///
/// 交互要点：
/// - 进入时拉轨迹回放；会话仍在运行则自动接上事件流（离开再回来不重拉，继续跟随）；
/// - 生成中可继续打字，发送键变停止键；
/// - 自动滚动只在用户本就贴近底部时跟随新内容（上滚查看历史时不打断），离开底部给「回到最新消息」按钮；
/// - 「改写这条提问」只进入本地编辑态，发送时二次确认「替换并重新提问」才调用 retry；
/// - 模型 / 思维链选择器三态：没动过（发送时不传，服务端沿用上一条）/ 显式默认 / 显式值，
///   展示值回落到会话最近一轮的轨迹信封值。
struct AgentConversationView: View {
    let sessionId: String

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @Environment(Feedback.self) private var feedback

    @State private var conversation: AgentConversation
    @State private var draft = AgentDraft()
    /// 外层 nil = 没动过选择器；.some(nil) = 显式「默认」
    @State private var thinkingChoice: String??
    @State private var modelChoice: String??
    @State private var modelOptions: [API.LlmModelOptionView] = []
    @State private var knownSkills: Set<String>?
    /// false = 明确未接入模型（锁定追问并引导去设置）
    @State private var llmConfigured: Bool?
    /// 正在改写的提问（message_id）
    @State private var retryTarget: String?
    @State private var retrying = false
    @State private var position = ScrollPosition(edge: .bottom)
    @State private var nearBottom = true

    init(sessionId: String) {
        self.sessionId = sessionId
        _conversation = State(initialValue: AgentConversationRegistry.conversation(sessionId))
    }

    var body: some View {
        content
            .background(Theme.background.ignoresSafeArea())
            .navigationTitle(conversation.loaded ? conversation.title : "AI 会话")
            .navigationBarTitleDisplayMode(.inline)
            .hidesTabBar()
            .toolbar {
                // 右上角只留「⋯」，操作的是当前会话；Web 顶栏的搜索与「+」在会话页里用不上（2026-09-26 用户决定去掉）
                if conversation.loaded {
                    ToolbarItem(placement: .topBarTrailing) { sessionMenu }
                }
            }
            .environment(\.openURL, OpenURLAction { url in
                // 站内链接（相对路径或指向当前服务器）走原生路由；外链交给系统浏览器（同 Web 新窗口打开）
                if url.scheme == nil || url.host() == api.server.apiBase.host(), router.open(webPath: url.path() + (url.query().map { "?\($0)" } ?? "")) {
                    return .handled
                }
                return .systemAction
            })
            .tracksSubscriptionIndex()
            .task(id: sessionId) {
                await conversation.open(api: api)
            }
            .task {
                async let configured = AgentCatalog.llmConfigured(api: api)
                async let options = AgentCatalog.modelOptions(api: api)
                async let skills = AgentCatalog.knownSkills(api: api)
                llmConfigured = await configured
                modelOptions = await options
                knownSkills = await skills
            }
            .onDisappear { conversation.detachIfIdle() }
    }

    @ViewBuilder
    private var content: some View {
        if !conversation.loaded, let error = conversation.loadError {
            VStack(spacing: 8) {
                Text("无法打开会话").font(.system(size: 17, weight: .medium)).foregroundStyle(Theme.text)
                Text(error).font(.system(size: 14)).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
            }
            .padding(.horizontal, 24)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .accessibilityIdentifier("agent-load-error")
        } else if !conversation.loaded {
            Text("正在加载会话…")
                .font(.system(size: 16))
                .foregroundStyle(Theme.textMuted)
                .modifier(AgentPulse(active: true))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityIdentifier("agent-loading")
        } else {
            transcript
                .safeAreaInset(edge: .bottom, spacing: 0) { composerArea }
        }
    }

    // MARK: 消息列

    private var transcript: some View {
        let running = conversation.running
        return ScrollView {
            LazyVStack(alignment: .leading, spacing: 32) {
                if let handoff = conversation.handoff {
                    AgentHandoffCard(sourceId: handoff.sourceId, sourceTitle: handoff.sourceTitle)
                }
                ForEach(conversation.turns) { turn in
                    // 运行中不给改写入口：服务端会拒绝替换正在写轨迹的会话
                    AgentTurnView(
                        turn: turn,
                        sessionId: sessionId,
                        knownSkills: knownSkills,
                        onEdit: running || retrying ? nil : { messageId, input in
                            retryTarget = messageId
                            draft.load(input)
                        }
                    )
                    .equatable()
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 16)
        }
        .scrollPosition($position)
        .defaultScrollAnchor(.bottom, for: .initialOffset)
        .scrollDismissesKeyboard(.interactively)
        .scrollEdgeEffectStyle(.hard, for: .bottom)
        .onScrollGeometryChange(for: Bool.self) { geometry in
            geometry.contentSize.height - geometry.visibleRect.maxY < 120
        } action: { _, near in
            nearBottom = near
        }
        .onChange(of: conversation.turns) {
            if nearBottom { position.scrollTo(edge: .bottom) }
        }
        .overlay(alignment: .bottom) {
            if !nearBottom {
                Button {
                    withAnimation { position.scrollTo(edge: .bottom) }
                } label: {
                    Image(systemName: "chevron.down")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Theme.textMuted)
                        .frame(width: 36, height: 36)
                        .background(Color(red: 0x23 / 255, green: 0x23 / 255, blue: 0x25 / 255), in: .circle)
                        .overlay(Circle().strokeBorder(Color.white.opacity(0.1)))
                        .shadow(color: .black.opacity(0.4), radius: 8, y: 3)
                        .contentShape(Circle().inset(by: -4))
                }
                .buttonStyle(.plain)
                .padding(.bottom, 10)
                .accessibilityLabel("回到最新消息")
                .accessibilityIdentifier("agent-scroll-latest")
            }
        }
    }

    // MARK: 底部输入

    private var composerArea: some View {
        let turns = conversation.turns
        // 会话当前生效的档位 / 模型 = 最近一轮有值的轨迹信封值
        let sessionThinking = turns.last(where: { $0.thinkingLevel != nil })?.thinkingLevel ?? nil
        let sessionModel = turns.last(where: { $0.modelRef != nil })?.modelRef ?? nil
        let displayedThinking = thinkingChoice ?? sessionThinking
        let displayedModel = modelChoice ?? sessionModel
        let locked = llmConfigured == false

        return VStack(alignment: .leading, spacing: 8) {
            if retryTarget != nil {
                HStack {
                    Text("正在改写较早的提问，发送后将替换其后的对话")
                    Spacer()
                    Button("取消改写") {
                        retryTarget = nil
                        draft.clear()
                    }
                    .foregroundStyle(Theme.textFaint)
                    .accessibilityIdentifier("agent-cancel-retry")
                }
                .font(.system(size: 13))
                .foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 8)
            }
            AgentComposer(
                draft: $draft,
                placeholder: locked ? "请先接入 AI 模型，再继续对话"
                    : retryTarget != nil ? "修改问题后发送，将从这里重新生成回答" : nil,
                busy: conversation.running,
                disabled: locked || (retrying && retryTarget != nil),
                // 改写模式不开图片入口：retry 沿用原消息的图，新加的图无处安放，藏起入口比静默丢弃诚实
                imageUpload: retryTarget == nil,
                modelOptions: modelOptions,
                modelValue: displayedModel,
                onModelChange: { ref in
                    modelChoice = .some(ref)
                    // 换模型后旧档位可能不在新菜单里，显式清回默认
                    thinkingChoice = .some(nil)
                    AgentComposerPrefs(model: ref, thinking: nil).save()
                },
                thinkingValue: displayedThinking,
                onThinkingChange: { level in
                    thinkingChoice = .some(level)
                    AgentComposerPrefs(model: displayedModel, thinking: level).save()
                },
                onSubmit: submit,
                onStop: stop
            )
            if locked { AgentLLMSetupNotice() }
        }
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 8)
        .background {
            // 沉浸页纯色底：输入区不透出下方消息（同 Web 贴底输入行）
            Theme.background.ignoresSafeArea(edges: .bottom)
        }
    }

    /// 线上值：没动过不传（服务端沿用）；显式默认传 "default"
    private func wire(_ choice: String??) -> String? {
        switch choice {
        case .none: nil
        case .some(.none): "default"
        case let .some(.some(value)): value
        }
    }

    private func submit() {
        let message = draft.message
        let images = draft.images.map { AgentTurnImage(attachmentId: $0.attachmentId, name: $0.name) }
        guard let target = retryTarget else {
            draft.clear()
            nearBottom = true
            conversation.send(api: api, input: message, images: images, thinking: wire(thinkingChoice), model: wire(modelChoice))
            position.scrollTo(edge: .bottom)
            return
        }
        Task {
            let agreed = await feedback.confirm(
                "重新提交这条提问？",
                message: "这条提问及其之后的对话会被新问题替换，原记录无法恢复。",
                confirmTitle: "替换并重新提问",
                destructive: true
            )
            guard agreed else { return }
            retrying = true
            defer { retrying = false }
            do {
                // 改写模式下选择器照常可用：没动过就沿用被重试消息的值
                try await conversation.retry(api: api, messageId: target, content: message, thinking: wire(thinkingChoice), model: wire(modelChoice))
                draft.clear()
                retryTarget = nil
                nearBottom = true
                position.scrollTo(edge: .bottom)
            } catch {
                feedback.error(error)
            }
        }
    }

    // MARK: 会话菜单

    /// 当前会话的操作：从此处创建新会话（聊天记录页最常用，放第一位）/ 重命名 / 删除
    /// （「更多」页最近会话的行尾菜单与此同图标、同顺序）
    private var sessionMenu: some View {
        Menu {
            Button("从此处创建新会话", systemImage: "arrow.triangle.branch") { Task { await fork() } }
            Button("重命名", systemImage: "pencil") { Task { await rename() } }
            Divider()
            Button("删除会话", systemImage: "trash", role: .destructive) { Task { await remove() } }
        } label: {
            Image(systemName: "ellipsis")
        }
        .accessibilityLabel("会话操作")
        .accessibilityIdentifier("agent-session-menu")
    }

    /// 从此处创建新会话：服务端带上本会话的上下文开一个新会话（同 Web「在新会话中继续」），打开后接着聊
    private func fork() async {
        do {
            let forked = try await api.sessionFork(sessionId: sessionId)
            router.open(.session(id: forked.session.id))
        } catch {
            feedback.error("创建新会话失败：\(error.localizedDescription)")
        }
    }

    private func rename() async {
        // 初值是顶栏显示的标题；去空白、截 80 字，没变化就不发请求（同 Web）
        let current = conversation.title
        guard let input = await feedback.prompt("重命名会话", placeholder: "会话标题（最多 80 字）", initial: current, maxLength: 80) else { return }
        let name = String(input.trimmingCharacters(in: .whitespacesAndNewlines).prefix(80))
        guard !name.isEmpty, name != current else { return }
        do {
            let updated = try await api.sessionRename(sessionId: sessionId, body: .init(title: name))
            conversation.title = updated.title ?? name
        } catch {
            feedback.error("重命名失败：\(error.localizedDescription)")
        }
    }

    private func remove() async {
        let title = conversation.title.isEmpty ? "未命名会话" : conversation.title
        guard await feedback.confirm(
            "彻底删除会话「\(title)」？",
            message: "服务器上的完整对话记录将一并删除，此操作不可恢复。",
            confirmTitle: "彻底删除", destructive: true
        ) else { return }
        do {
            _ = try await api.sessionDelete(sessionId: sessionId)
            feedback.success("会话已删除")
            router.pop()
        } catch {
            feedback.error("删除失败：\(error.localizedDescription)")
        }
    }

    private func stop() {
        Task {
            do {
                try await conversation.stop(api: api)
            } catch {
                // 停止失败时 Agent 可能仍在执行，不在客户端伪造终态；真实终态仍会经事件流落到界面
                feedback.error("停止失败，请稍后重试：\(error.localizedDescription)")
            }
        }
    }
}

/// 未接入模型时的公共引导（对应 Web `LlmSetupNotice`）：说明能解锁什么，并给唯一的设置入口
struct AgentLLMSetupNotice: View {
    @Environment(Router.self) private var router

    var body: some View {
        HStack(spacing: 4) {
            Text("接入 AI 模型后即可解锁AI 对话能力。")
                .foregroundStyle(Theme.textMuted)
            Button("去接入") { router.push(.settingsSection(.llm)) }
                .foregroundStyle(Theme.accent)
                .accessibilityIdentifier("agent-llm-setup")
        }
        .font(.system(size: 14))
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(Theme.surfaceRaised, in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.line))
    }
}
