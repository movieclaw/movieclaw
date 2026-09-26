import Foundation

// AI 会话的展示时间线（纯逻辑，对应 Web `lib/agent-conversations.tsx` 的前半部分）。
//
// 事实源在服务端（JSONL 轨迹 + SQLite 索引），这里只是它的渲染形态：
// - 「一轮」= 一条 user 消息 + Agent 的完整产出，是前端从消息序列派生的展示分组，不是协议实体；
// - 每轮产出是按发生顺序排列的「段」：处理过程块（思考 + 工具调用，折叠展示）与正文交替出现，
//   上下文压缩作为分隔卡片插在中间——与 agent loop 的实际执行顺序一致（仿 Claude 的呈现模型）；
// - 历史回放（轨迹 entries）与实时流（SSE 事件）归约到同一套结构，渲染层不区分来源。

/// 一次工具调用及其回执（tool_call_start 建条目、tool_call_delta 追加参数、tool_call 定稿、tool_result 补回执）
nonisolated struct AgentToolCall: Equatable, Sendable {
    var id: String
    /// 工具名（bash / mclaw / show_media_cards_v1…）；处理过程的状态与总结按它分类
    var name: String
    /// 展示用摘要 `name({...})`；参数生成中为逐片追加的半成品
    var label: String
    /// 定稿后的结构化参数；生成式 UI 按它绘制卡片
    var args: AgentJSONObject?
    /// 参数是否已生成完整；nil 视为已完整（回放数据）
    var argsDone: Bool?
    /// 执行回执；nil = 执行中
    var output: String?
    var isError = false

    /// 参数仍在流式生成（此时行只作进度展示、不可展开）
    var streaming: Bool { argsDone == false }
}

/// 处理过程条目：一段思维链或一次工具调用
nonisolated enum AgentProcessItem: Equatable, Sendable {
    case thinking(String)
    case tool(AgentToolCall)
}

/// 时间线段
nonisolated enum AgentSegment: Equatable, Sendable {
    case process([AgentProcessItem])
    case text(String)
    case compaction(summary: String, tokensBefore: Int?, tokensAfter: Int?)
}

/// 用户消息携带的一张图片（回放数据按 attachmentId 走附件下载接口；刚发出的用本地预览）
nonisolated struct AgentTurnImage: Equatable, Sendable, Hashable {
    var attachmentId: String
    var name: String?
}

/// 一轮对话
nonisolated struct AgentTurn: Equatable, Identifiable, Sendable {
    enum Status: Equatable, Sendable { case running, done, error }

    var id: String
    /// 开启本轮的 user message 稳定编号：「改写这条提问」的 retry 锚点（乐观轮次在服务端受理前为空）
    var messageId: String?
    /// 用户输入（显式技能调用还原成 `/skill:名字` token 形态）
    var input: String
    var images: [AgentTurnImage] = []
    /// 本轮生效的思维链档位：外层 nil = 未知（乐观轮次没显式指定），内层 nil = 模型默认
    var thinkingLevel: String??
    /// 本轮请求的模型引用，语义同上
    var modelRef: String??
    var status: Status
    var startedAt: Date
    /// 回放专用：本轮最后一条轨迹的时间，用于估算历史轮次耗时
    var endedAt: Date?
    var segments: [AgentSegment] = []
    var result: AgentRunResult?
    var error: String?
    /// 用户主动停止
    var stopped = false
    /// 回放派生：本轮没有以「无工具调用的正文」收尾（停机、崩溃、模型报错），页脚提示「已中断」
    var interrupted = false

    var isRunning: Bool { status == .running }

    /// 本轮全部正文（页脚「复制」用）
    var answerText: String {
        segments.compactMap { if case let .text(t) = $0 { t } else { nil } }
            .joined(separator: "\n\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

// MARK: - 段的归约规则

/// 思考/工具事件并入末尾的 process 块（没有则新开）；正文增量并入末尾 text 段（没有则新开）——
/// 由此天然形成「处理过程 ↔ 正文」交替的时间线。
enum AgentTimeline {
    static func appendThinking(_ turn: inout AgentTurn, _ delta: String) {
        guard !delta.isEmpty else { return }
        if case var .process(items) = turn.segments.last {
            if case let .thinking(text) = items.last {
                items[items.count - 1] = .thinking(text + delta)
            } else {
                items.append(.thinking(delta))
            }
            turn.segments[turn.segments.count - 1] = .process(items)
        } else {
            turn.segments.append(.process([.thinking(delta)]))
        }
    }

    static func appendText(_ turn: inout AgentTurn, _ delta: String) {
        guard !delta.isEmpty else { return }
        if case let .text(text) = turn.segments.last {
            turn.segments[turn.segments.count - 1] = .text(text + delta)
        } else {
            turn.segments.append(.text(delta))
        }
    }

    static func appendTool(_ turn: inout AgentTurn, _ tool: AgentToolCall) {
        if case var .process(items) = turn.segments.last {
            items.append(.tool(tool))
            turn.segments[turn.segments.count - 1] = .process(items)
        } else {
            turn.segments.append(.process([.tool(tool)]))
        }
    }

    /// 从后往前找到首个匹配的工具条目并打补丁；找到返回 true
    @discardableResult
    static func patchTool(_ turn: inout AgentTurn, where match: (AgentToolCall) -> Bool, _ patch: (inout AgentToolCall) -> Void) -> Bool {
        for s in turn.segments.indices.reversed() {
            guard case var .process(items) = turn.segments[s] else { continue }
            for i in items.indices.reversed() {
                guard case var .tool(tool) = items[i], match(tool) else { continue }
                patch(&tool)
                items[i] = .tool(tool)
                turn.segments[s] = .process(items)
                return true
            }
        }
        return false
    }

    /// 定稿标签 `name({...})`
    static func label(name: String, args: AgentJSONObject?, raw: String?) -> String {
        if let args, !args.isEmpty { return "\(name)(\(args.compactJSON))" }
        return "\(name)(\(raw ?? "{}"))"
    }

    // MARK: 实时事件

    /// 把一条 SSE 事件归约进本轮（与 Web applyAgentEvent 同规则）
    static func apply(_ event: AgentStreamEvent, to turn: inout AgentTurn) {
        switch event.type {
        case "agent_start":
            break
        case "thinking_delta":
            appendThinking(&turn, event.delta ?? "")
        case "text_delta":
            appendText(&turn, event.delta ?? "")
        case "tool_call_start":
            // 名称一确定就落条目，用户立刻看到「正在调用哪个工具」
            if let call = event.toolCall {
                appendTool(&turn, AgentToolCall(id: call.id, name: call.name, label: "\(call.name)(", argsDone: false))
            }
        case "tool_call_delta":
            guard let delta = event.delta, !delta.isEmpty else { return }
            // 按 id 归属增量；个别端点分片不带 id 时，退化为最后一个参数未完成的调用
            let hit = patchTool(&turn, where: { $0.id == event.toolCallId }) { $0.label += delta }
            if !hit { patchTool(&turn, where: { $0.argsDone == false }) { $0.label += delta } }
        case "tool_call":
            guard let call = event.toolCall else { return }
            let label = label(name: call.name, args: call.arguments, raw: call.rawArguments)
            let hit = patchTool(&turn, where: { $0.id == call.id }) {
                $0.label = label
                $0.args = call.arguments
                $0.argsDone = true
            }
            if !hit {
                appendTool(&turn, AgentToolCall(id: call.id, name: call.name, label: label, args: call.arguments, argsDone: true))
            }
        case "tool_result":
            guard let result = event.toolResult else { return }
            patchTool(&turn, where: { $0.id == result.toolCallId }) {
                $0.output = result.output
                $0.isError = result.isError ?? false
            }
        case "context_compacted":
            if let c = event.compaction {
                turn.segments.append(.compaction(summary: c.summary, tokensBefore: c.tokensBefore, tokensAfter: c.tokensAfter))
            }
        case "agent_done":
            turn.status = .done
            turn.result = event.result
        case "agent_error":
            turn.status = .error
            turn.error = event.error ?? "运行失败，原因未知"
        case "agent_cancelled":
            turn.status = .done
            turn.stopped = true
        default:
            break
        }
    }

    // MARK: 历史回放

    /// 轨迹 entries → 展示轮次：user 消息开启新一轮；assistant 的思考、正文、tool_calls 按
    /// 「thinking → text → 工具」并入时间线（与流式事件顺序一致）；tool 消息按 tool_call_id 合并进调用卡片。
    static func turns(from entries: [AgentEntry]) -> [AgentTurn] {
        var turns: [AgentTurn] = []
        // 每轮是否已正常收尾（终答 = 无 tool_calls 的 assistant 正文）
        var closed: [Bool] = []
        for entry in entries {
            switch entry {
            case .handoff, .unknown:
                // handoff 是会话级来源卡片，旧消息不在新会话里派生成可重试的轮次
                continue
            case let .compaction(c):
                guard !turns.isEmpty else { continue }
                turns[turns.count - 1].segments.append(.compaction(summary: c.summary, tokensBefore: c.tokensBefore, tokensAfter: c.tokensAfter))
                turns[turns.count - 1].endedAt = Formatters.date(c.timestamp)
            case let .message(m):
                let message = m.message
                if message.role == "user" {
                    turns.append(AgentTurn(
                        id: m.messageId,
                        messageId: m.messageId,
                        input: AgentSkillText.toTokenForm(message.text),
                        images: message.images,
                        thinkingLevel: .some(m.thinkingLevel),
                        modelRef: .some(m.model),
                        status: .done,
                        startedAt: Formatters.date(m.timestamp) ?? .now
                    ))
                    closed.append(false)
                    continue
                }
                guard !turns.isEmpty else { continue }
                var turn = turns[turns.count - 1]
                if message.role == "assistant" {
                    appendThinking(&turn, message.thinking)
                    appendText(&turn, message.text)
                    for call in message.toolCalls {
                        let args = (call.arguments?.isEmpty ?? true) ? nil : call.arguments
                        appendTool(&turn, AgentToolCall(id: call.id, name: call.name, label: label(name: call.name, args: args, raw: call.rawArguments), args: args))
                    }
                    if m.finishReason == "aborted" { turn.stopped = true }
                    closed[turns.count - 1] = message.toolCalls.isEmpty && m.finishReason != "aborted"
                } else if message.role == "tool" {
                    let text = message.text
                    patchTool(&turn, where: { $0.id == message.toolCallId }) { $0.output = text }
                }
                turn.endedAt = Formatters.date(m.timestamp)
                turns[turns.count - 1] = turn
            }
        }
        for i in turns.indices where !closed[i] && !turns[i].stopped {
            turns[i].interrupted = true
        }
        return turns
    }

    // MARK: 处理过程块的文案

    /// 进行中的处理块此刻在做什么：取末尾条目推导
    static func processStatus(_ items: [AgentProcessItem]) -> String {
        guard case let .tool(tail) = items.last else { return "思考中…" }
        if tail.argsDone == false { return "准备调用 \(tail.name)…" }
        if tail.output == nil { return "正在执行 \(tail.name)…" }
        return "思考中…" // 工具已返回，正在等模型的下一步
    }

    /// 已完成处理块的一句话总结，如「已思考，执行 1 次命令，调用 2 次工具」
    static func processSummary(_ items: [AgentProcessItem]) -> String {
        var hasThinking = false
        var commands = 0
        var others = 0
        for item in items {
            switch item {
            case .thinking: hasThinking = true
            case let .tool(tool): if tool.name == "bash" { commands += 1 } else { others += 1 }
            }
        }
        var parts: [String] = []
        if hasThinking { parts.append("思考") }
        if commands > 0 { parts.append("执行 \(commands) 次命令") }
        if others > 0 { parts.append("调用 \(others) 次工具") }
        return parts.isEmpty ? "处理过程" : "已" + parts.joined(separator: "，")
    }

    /// 耗时文案：不足一分钟按秒（终态保留一位小数），超过则「x 分 y 秒」
    static func duration(_ seconds: Double, precise: Bool) -> String {
        let s = max(0, seconds)
        if s < 60 { return precise ? String(format: "%.1fs", s) : "\(Int(s))s" }
        let minutes = Int(s / 60)
        return "\(minutes) 分 \(Int(s) - minutes * 60) 秒"
    }
}

// MARK: - 工具参数的展示

extension AgentToolCall {
    /// 展开后的参数明细：bash 取 command 按 shell 展示，其余美化 JSON；解析失败原样展示括号内文本
    var inputDetail: (code: String, lang: AgentCodeLanguage)? {
        if let args {
            if name == "bash", let command = args["command"]?.stringValue, case .string = args["command"] {
                return (command, .bash)
            }
            return args.isEmpty ? nil : (args.prettyJSON, .json)
        }
        let raw = rawInner
        return raw.isEmpty || raw == "{}" ? nil : (raw, .json)
    }

    /// 单行参数摘要（清单态）：bash 压成一行命令，其余压成「键: 值」串
    var summary: String {
        if let args {
            if name == "bash", case let .string(command)? = args["command"] {
                return command.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            }
            return args.keys.map { key in
                let value = args[key]!
                if case let .string(s) = value { return "\(key): \(s)" }
                return "\(key): \(AgentJSONText.render(value, pretty: false))"
            }.joined(separator: ", ")
        }
        let raw = rawInner
        return raw == "{}" ? "" : raw
    }

    /// 标签括号里的原始参数文本
    var rawInner: String {
        guard label.count > name.count + 1 else { return "" }
        var inner = label.dropFirst(name.count + 1)
        if inner.hasSuffix(")") { inner = inner.dropLast() }
        return String(inner)
    }

    /// 已失败：流式期间由回执标记，回放数据靠 runner 的失败前缀识别
    var failed: Bool { isError || (output?.hasPrefix("工具执行失败：") ?? false) }
}
