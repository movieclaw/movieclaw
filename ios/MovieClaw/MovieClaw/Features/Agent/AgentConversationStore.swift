import SwiftUI

/// 一个 AI 会话的客户端状态（对应 Web `lib/agent-conversations.tsx` 的 store 部分）。
///
/// 设计要点：
/// - **服务端是事实源**：打开会话时拉完整轨迹回放成展示轮次；本对象只是渲染缓存。
/// - **运行与连接解耦**：后台运行不依赖本连接——关页面、切后台都不会取消任务；事件流断了按事件序号
///   （`Last-Event-ID`）续传，服务端只补发缺失部分；只有 HTTP 错误（如运行记录过期 404）才结束跟随。
/// - **运行中的会话从事件 0 完整回放**：轨迹快照与事件游标不同步，正在运行那一轮丢弃已落盘的局部产出，
///   整轮交给事件流重建，避免重复。
/// - **事件批量合并**：流式时 text_delta 每秒几十个，逐个改状态会让整条时间线同频重绘；
///   这里按 80ms 合并应用一次（视觉上仍是打字机效果），终态事件立即冲刷。
/// - 新任务页创建会话后把带乐观轮次的对象放进 `AgentConversationRegistry`，会话页直接接手，
///   不必等轨迹回放（与 Web SPA 导航不重拉已加载会话同理）。
@Observable
final class AgentConversation {
    let id: String
    var title: String
    var turns: [AgentTurn] = []
    /// 从旧会话续开时的来源（只用于展示来源卡片）
    var handoff: (sourceId: String, sourceTitle: String?)?
    /// 详情是否已从服务端回放（或由新任务页乐观建立）
    var loaded = false
    /// 首次加载失败的原因（404 = 会话不存在）
    var loadError: String?

    @ObservationIgnored private var streamTask: Task<Void, Never>?
    @ObservationIgnored private var pending: [(turnId: String, event: AgentStreamEvent)] = []
    @ObservationIgnored private var flushTask: Task<Void, Never>?
    @ObservationIgnored private var loading = false

    init(id: String, title: String = "") {
        self.id = id
        self.title = title
    }

    var running: Bool { turns.contains(where: \.isRunning) }

    /// 仍连着事件流（离开页面再回来时不必重拉轨迹）
    var following: Bool { streamTask != nil }

    // MARK: 打开

    /// 打开会话：拉轨迹回放；会话仍在运行时自动接上事件流。已在跟随中的直接复用。
    func open(api: APIClient) async {
        // 跟随中 / 正在加载 / 本地乐观轮次还没接上事件流时都不重拉，免得把乐观轮次覆盖掉
        if following || loading || (loaded && running) { return }
        loading = true
        defer { loading = false }
        do {
            let detail = try await api.agentTranscript(sessionId: id)
            apply(detail)
            loadError = nil
            if detail.session.running, let last = turns.last {
                connect(api: api, turnId: last.id)
            }
        } catch is CancellationError {
        } catch {
            // 已有内容时静默保留（下次进入再刷新），没有内容才进失败态
            if !loaded { loadError = error.localizedDescription }
        }
    }

    private func apply(_ detail: AgentTranscript) {
        let summary = detail.session
        var turns = AgentTimeline.turns(from: detail.entries)
        if summary.running, !turns.isEmpty {
            let i = turns.count - 1
            turns[i].status = .running
            turns[i].segments = []
            turns[i].endedAt = nil
            turns[i].result = nil
            turns[i].error = nil
            turns[i].stopped = false
            turns[i].interrupted = false
            turns[i].startedAt = Date.now
        }
        self.turns = turns
        title = Self.title(of: summary)
        handoff = detail.entries.lazy.compactMap { entry -> (String, String?)? in
            if case let .handoff(h) = entry { return (h.sourceSessionId, h.sourceTitle) }
            return nil
        }.first.map { (sourceId: $0.0, sourceTitle: $0.1) }
        loaded = true
    }

    static func title(of summary: API.SessionSummary) -> String {
        if let title = summary.title, !title.isEmpty { return title }
        if let prompt = summary.lastPrompt, !prompt.isEmpty { return prompt }
        return "未命名会话"
    }

    // MARK: 发送 / 停止 / 改写重问

    /// 在既有会话中追问一轮：先落乐观轮次，服务端受理后接上事件流。
    /// thinking / model 是线上值：nil = 沿用会话上一条，"default" = 清回默认，其余为显式值。
    func send(api: APIClient, input: String, images: [AgentTurnImage], thinking: String?, model: String?) {
        let turnId = UUID().uuidString
        var turn = AgentTurn(id: turnId, input: input, images: images, status: .running, startedAt: .now)
        if let thinking { turn.thinkingLevel = .some(thinking == "default" ? nil : thinking) }
        if let model { turn.modelRef = .some(model == "default" ? nil : model) }
        // 会话被截空后重新开口的第一句，同服务端规则给它命名
        if turns.isEmpty { title = String(input.prefix(30)) }
        turns.append(turn)
        Task {
            do {
                let accepted = try await api.sessionStart(body: .init(
                    content: input,
                    attachments: images.isEmpty ? nil : images.map(\.attachmentId),
                    sessionId: id,
                    model: model,
                    thinkingLevel: thinking
                ))
                updateTurn(turnId) { $0.messageId = accepted.messageId }
                connect(api: api, turnId: turnId)
            } catch {
                updateTurn(turnId) {
                    $0.status = .error
                    $0.error = error.localizedDescription
                }
            }
        }
    }

    /// 请求停止当前轮次。真正的终态由事件流里的 agent_cancelled 确认，这里不伪造终态。
    func stop(api: APIClient) async throws {
        _ = try await api.sessionStop(sessionId: id)
    }

    /// 改写重问：服务端一次完成旧轨迹丢弃与新消息提交，成功后本地才替换时间线；失败保留原对话。
    func retry(api: APIClient, messageId: String, content: String, thinking: String?, model: String?) async throws {
        guard let index = turns.firstIndex(where: { $0.messageId == messageId }) else {
            throw APIError.network("这条提问已不在当前会话里")
        }
        let original = turns[index]
        let accepted = try await api.sessionRetry(sessionId: id, body: .init(
            messageId: messageId, content: content, attachments: nil, model: model, thinkingLevel: thinking
        ))
        var turn = AgentTurn(
            id: UUID().uuidString, messageId: accepted.messageId, input: content,
            images: original.images, status: .running, startedAt: .now
        )
        // 不传档位/模型即沿用原消息的值；显式传了就用线上值（"default" 即 nil）
        turn.thinkingLevel = thinking.map { .some($0 == "default" ? nil : $0) } ?? original.thinkingLevel
        turn.modelRef = model.map { .some($0 == "default" ? nil : $0) } ?? original.modelRef
        if index == 0 { title = String(content.prefix(30)) }
        turns = Array(turns.prefix(index)) + [turn]
        connect(api: api, turnId: turn.id)
    }

    // MARK: 事件流

    /// 跟随会话当前一轮：网络中断指数退避续传（0.5s 起、最长 5s），HTTP 错误才结束。
    func connect(api: APIClient, turnId: String) {
        streamTask?.cancel()
        let path = "/sessions/\(id)/events"
        streamTask = Task { [weak self] in
            var lastId = 0
            var delay = 0.5
            while !Task.isCancelled {
                do {
                    let stream = api.events(path, lastEventId: lastId > 0 ? String(lastId) : nil)
                    for try await raw in stream {
                        // 注释心跳没有 id；重放的旧事件（id 不大于游标）跳过
                        guard let seq = raw.id.flatMap(Int.init), seq > lastId else { continue }
                        guard let event = try? raw.decode(AgentStreamEvent.self) else { lastId = seq; continue }
                        lastId = seq
                        delay = 0.5
                        self?.enqueue(turnId: turnId, event: event)
                        if event.isTerminal {
                            self?.finishStream(api: api)
                            return
                        }
                    }
                } catch let APIError.http(status, message, _) {
                    self?.flush()
                    self?.updateTurn(turnId) {
                        $0.status = .error
                        $0.error = status == 404 ? "运行记录不存在或已过期，可能是服务已重启，请重新发起任务" : message
                    }
                    self?.streamTask = nil
                    return
                } catch {
                    if Task.isCancelled { return }
                    // 网络错误：保留游标，退避后重新 GET，服务端只补发尚未确认的事件
                }
                if Task.isCancelled { return }
                try? await Task.sleep(for: .seconds(delay))
                delay = min(delay * 2, 5)
            }
        }
    }

    /// 本轮结束：冲刷缓冲；顺带刷新标题（服务端可能在首轮结束后自动命名）
    private func finishStream(api: APIClient) {
        flush()
        streamTask = nil
        Task {
            guard let list = try? await api.sessionList(limit: 20),
                  let summary = list.first(where: { $0.id == id }) else { return }
            title = Self.title(of: summary)
        }
    }

    /// 离开会话页且本轮已结束时释放连接（运行中的保持跟随，回来时无需重拉）
    func detachIfIdle() {
        if !running { streamTask?.cancel(); streamTask = nil }
    }

    private func enqueue(turnId: String, event: AgentStreamEvent) {
        pending.append((turnId, event))
        if event.isTerminal {
            flush()
        } else if flushTask == nil {
            flushTask = Task { [weak self] in
                try? await Task.sleep(for: .milliseconds(80))
                self?.flush()
            }
        }
    }

    private func flush() {
        flushTask?.cancel()
        flushTask = nil
        guard !pending.isEmpty else { return }
        var turns = self.turns
        for (turnId, event) in pending {
            guard let i = turns.firstIndex(where: { $0.id == turnId }) else { continue }
            AgentTimeline.apply(event, to: &turns[i])
        }
        pending.removeAll()
        self.turns = turns
    }

    private func updateTurn(_ turnId: String, _ patch: (inout AgentTurn) -> Void) {
        guard let i = turns.firstIndex(where: { $0.id == turnId }) else { return }
        patch(&turns[i])
    }
}

/// 会话对象的进程内登记表：新任务页创建的会话与会话页共享同一个对象（乐观轮次不丢）。
@MainActor
enum AgentConversationRegistry {
    private static var items: [String: AgentConversation] = [:]

    static func conversation(_ id: String) -> AgentConversation {
        if let hit = items[id] { return hit }
        let created = AgentConversation(id: id)
        items[id] = created
        return created
    }

    /// 新建服务端会话并发起首轮运行，成功后返回会话 id（调用方跳转 /sessions/{id}）。
    /// 必须等服务端分配 session_id 才有路由地址，因此这一步同步等待；失败直接抛给调用方。
    static func start(api: APIClient, input: String, images: [AgentTurnImage], thinking: String?, model: String?) async throws -> String {
        let accepted = try await api.sessionStart(body: .init(
            content: input,
            attachments: images.isEmpty ? nil : images.map(\.attachmentId),
            sessionId: nil,
            model: model,
            thinkingLevel: thinking
        ))
        // 标题取首轮输入前 30 字（技能 token 折叠成 [技能]，纯图消息占位 [图片]），与服务端索引同口径
        let parsed = AgentSkillText.parseTokens(input)
        let optimistic = parsed.names.isEmpty ? input : "[技能] \(parsed.text)".trimmingCharacters(in: .whitespaces)
        let title = String(optimistic.prefix(30))
        let conversation = AgentConversation(id: accepted.sessionId, title: title.isEmpty ? (images.isEmpty ? "" : "[图片]") : title)
        var turn = AgentTurn(id: UUID().uuidString, messageId: accepted.messageId, input: input, images: images, status: .running, startedAt: .now)
        if let thinking { turn.thinkingLevel = .some(thinking == "default" ? nil : thinking) }
        if let model { turn.modelRef = .some(model == "default" ? nil : model) }
        conversation.turns = [turn]
        conversation.loaded = true
        items[accepted.sessionId] = conversation
        conversation.connect(api: api, turnId: turn.id)
        return accepted.sessionId
    }
}

/// 刚发出的图片的本地预览（乐观气泡直接用，不回读服务端）；按附件编号索引，进程内有效
@MainActor
enum AgentImagePreviews {
    static var images: [String: UIImage] = [:]
}
