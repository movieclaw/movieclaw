import Foundation

// AI 会话模块的手写接口与线上数据结构（对应 Web `lib/api/agent.ts`）。
//
// 为什么不全用生成接口：
// - 会话轨迹 `GET /sessions/{id}` 的 entries 是 message / compaction / handoff 三种形态的联合，
//   生成器只能给出 `[API.JSONValue]`；长会话动辄几百条、单条工具输出几 KB，先解成 JSONValue
//   再二次解码代价大，这里直接按判别字段解成本模块的结构体；
// - 事件流 `GET /sessions/{id}/events`（SSE）与附件上传（multipart）生成器本就跳过。
// 其余接口（开始/继续、停止、改写重问、技能、模型清单）仍用生成的函数。

// MARK: - 会话轨迹

/// 会话详情：摘要 + 按写入顺序排列的轨迹 entries
nonisolated struct AgentTranscript: Decodable, Sendable {
    var session: API.SessionSummary
    var entries: [AgentEntry]
}

/// 轨迹里的一行。未知 type（更新的后端）解成 `.unknown`，渲染时跳过，不让整页解码失败。
nonisolated enum AgentEntry: Decodable, Sendable {
    case message(AgentMessageEntry)
    case compaction(AgentCompactionEntry)
    case handoff(AgentHandoffEntry)
    case unknown

    private enum Keys: String, CodingKey { case type }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: Keys.self)
        switch try container.decodeIfPresent(String.self, forKey: .type) {
        case "message": self = .message(try AgentMessageEntry(from: decoder))
        case "compaction": self = .compaction(try AgentCompactionEntry(from: decoder))
        case "handoff": self = .handoff(try AgentHandoffEntry(from: decoder))
        default: self = .unknown
        }
    }
}

/// 一条消息 entry：信封（编号、时间、运行元数据）+ LLM API 原样消息
nonisolated struct AgentMessageEntry: Decodable, Sendable {
    var messageId: String
    var timestamp: String
    var message: AgentWireMessage
    /// user 行：本轮请求的模型引用（null = 默认模型）；assistant 行：供应商回报的实际模型 id
    var model: String?
    /// 约定含 "aborted"：该步产出时运行被取消
    var finishReason: String?
    /// user 行生效的思维链档位（null = 模型默认）
    var thinkingLevel: String?

    enum CodingKeys: String, CodingKey {
        case messageId = "message_id"
        case timestamp, message, model
        case finishReason = "finish_reason"
        case thinkingLevel = "thinking_level"
    }
}

/// LLM API 格式的消息（按 role 分发渲染）
nonisolated struct AgentWireMessage: Decodable, Sendable {
    var role: String
    var content: [AgentContentPart]
    var toolCalls: [AgentWireToolCall]
    var toolCallId: String?

    enum CodingKeys: String, CodingKey {
        case role, content
        case toolCalls = "tool_calls"
        case toolCallId = "tool_call_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        role = try c.decode(String.self, forKey: .role)
        // content 可能是纯字符串，也可能是内容块数组
        if let text = try? c.decodeIfPresent(String.self, forKey: .content) {
            content = [AgentContentPart(type: "text", text: text)]
        } else {
            content = (try? c.decodeIfPresent([AgentContentPart].self, forKey: .content)) ?? []
        }
        toolCalls = (try? c.decodeIfPresent([AgentWireToolCall].self, forKey: .toolCalls)) ?? []
        toolCallId = try? c.decodeIfPresent(String.self, forKey: .toolCallId)
    }

    /// 正文纯文本（text 块拼接）
    var text: String { content.filter { $0.type == "text" }.compactMap(\.text).joined() }
    /// 思考内容（thinking 块拼接，仅 assistant）
    var thinking: String { content.filter { $0.type == "thinking" }.compactMap(\.text).joined() }
    /// 用户消息里的图片引用（没有 attachment_id 的历史块跳过）
    var images: [AgentTurnImage] {
        content.compactMap { part in
            guard part.type == "image", let id = part.attachmentId else { return nil }
            return AgentTurnImage(attachmentId: id, name: part.name)
        }
    }
}

nonisolated struct AgentContentPart: Decodable, Sendable {
    var type: String
    var text: String?
    var attachmentId: String?
    var name: String?

    enum CodingKeys: String, CodingKey {
        case type, text, name
        case attachmentId = "attachment_id"
    }

    init(type: String, text: String?) {
        self.type = type
        self.text = text
    }
}

/// 轨迹里的一次工具调用（参数已由协议层解析为对象；解析失败时带原始串）
nonisolated struct AgentWireToolCall: Decodable, Sendable {
    var id: String
    var name: String
    var arguments: AgentJSONObject?
    var rawArguments: String?

    enum CodingKeys: String, CodingKey {
        case id, name, arguments
        case rawArguments = "raw_arguments"
    }
}

nonisolated struct AgentCompactionEntry: Decodable, Sendable {
    var timestamp: String
    var summary: String
    var tokensBefore: Int?
    var tokensAfter: Int?

    enum CodingKeys: String, CodingKey {
        case timestamp, summary
        case tokensBefore = "tokens_before"
        case tokensAfter = "tokens_after"
    }
}

nonisolated struct AgentHandoffEntry: Decodable, Sendable {
    var sourceSessionId: String
    var sourceTitle: String?

    enum CodingKeys: String, CodingKey {
        case sourceSessionId = "source_session_id"
        case sourceTitle = "source_title"
    }
}

/// 保留键顺序的 JSON 对象。
///
/// 工具参数要按模型给出的顺序展示（Web 的 JSON.stringify 保序）；Swift 字典无序，
/// 所以这里另存一份键序。取值仍按字典查。
nonisolated struct AgentJSONObject: Decodable, Sendable, Equatable {
    var keys: [String]
    var values: [String: API.JSONValue]

    private struct AnyKey: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        var keys: [String] = []
        var values: [String: API.JSONValue] = [:]
        for key in c.allKeys {
            keys.append(key.stringValue)
            values[key.stringValue] = try c.decode(API.JSONValue.self, forKey: key)
        }
        self.keys = keys
        self.values = values
    }

    subscript(key: String) -> API.JSONValue? { values[key] }
    var isEmpty: Bool { keys.isEmpty }

    /// 紧凑 JSON（`name({...})` 标签里用；与 Web JSON.stringify 同形）
    var compactJSON: String { AgentJSONText.render(.object(values), order: keys, pretty: false) }
    /// 两空格缩进的美化 JSON（展开工具调用时展示参数）
    var prettyJSON: String { AgentJSONText.render(.object(values), order: keys, pretty: true) }
}

/// JSONValue → 文本（自带的 JSONEncoder 会打乱键序、转义中文，展示用自己拼）
nonisolated enum AgentJSONText {
    static func render(_ value: API.JSONValue, order: [String]? = nil, pretty: Bool, indent: Int = 0) -> String {
        let pad = String(repeating: "  ", count: indent + 1)
        let closePad = String(repeating: "  ", count: indent)
        switch value {
        case .null: return "null"
        case let .bool(b): return b ? "true" : "false"
        case let .int(i): return String(i)
        case let .double(d): return d == d.rounded() && abs(d) < 1e15 ? String(Int(d)) : String(d)
        case let .string(s): return quote(s)
        case let .array(items):
            if items.isEmpty { return "[]" }
            let parts = items.map { render($0, pretty: pretty, indent: indent + 1) }
            return pretty ? "[\n" + parts.map { pad + $0 }.joined(separator: ",\n") + "\n\(closePad)]" : "[" + parts.joined(separator: ",") + "]"
        case let .object(dict):
            if dict.isEmpty { return "{}" }
            let keys = (order ?? []).filter { dict[$0] != nil } + dict.keys.filter { !(order ?? []).contains($0) }.sorted()
            let parts = keys.map { key in
                quote(key) + (pretty ? ": " : ":") + render(dict[key]!, pretty: pretty, indent: indent + 1)
            }
            return pretty ? "{\n" + parts.map { pad + $0 }.joined(separator: ",\n") + "\n\(closePad)}" : "{" + parts.joined(separator: ",") + "}"
        }
    }

    static func quote(_ s: String) -> String {
        var out = "\""
        for ch in s.unicodeScalars {
            switch ch {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            default:
                if ch.value < 0x20 { out += String(format: "\\u%04x", ch.value) } else { out.unicodeScalars.append(ch) }
            }
        }
        return out + "\""
    }
}

// MARK: - 实时事件（SSE data 载荷，见后端 movieclaw_agent/events.py 的 AgentEvent）

nonisolated struct AgentStreamEvent: Decodable, Sendable {
    var type: String
    /// thinking_delta / text_delta / tool_call_delta 的增量文本
    var delta: String?
    /// tool_call_start：仅含 id/name；tool_call：参数完整的调用
    var toolCall: AgentWireToolCall?
    /// tool_call_delta：增量所属的工具调用 id
    var toolCallId: String?
    var toolResult: ToolResult?
    var compaction: Compaction?
    /// agent_start：实际路由到的供应商与模型
    var provider: String?
    var model: String?
    var result: AgentRunResult?
    var error: String?

    enum CodingKeys: String, CodingKey {
        case type, delta, provider, model, result, error, compaction
        case toolCall = "tool_call"
        case toolCallId = "tool_call_id"
        case toolResult = "tool_result"
    }

    struct ToolResult: Decodable, Sendable {
        var toolCallId: String
        var output: String
        var isError: Bool?
        var elapsedMs: Int?

        enum CodingKeys: String, CodingKey {
            case output
            case toolCallId = "tool_call_id"
            case isError = "is_error"
            case elapsedMs = "elapsed_ms"
        }
    }

    struct Compaction: Decodable, Sendable {
        var summary: String
        var tokensBefore: Int?
        var tokensAfter: Int?

        enum CodingKeys: String, CodingKey {
            case summary
            case tokensBefore = "tokens_before"
            case tokensAfter = "tokens_after"
        }
    }

    /// 终态事件：收到后本次运行结束，事件流不再重连
    var isTerminal: Bool { type == "agent_done" || type == "agent_error" || type == "agent_cancelled" }
}

/// agent_done 的终态载荷（usage 为全程累计）
nonisolated struct AgentRunResult: Decodable, Sendable, Equatable {
    var steps: Int?
    var model: String?
    var provider: String?
    var elapsedMs: Int
    var usage: Usage?

    struct Usage: Decodable, Sendable, Equatable {
        var promptTokens: Int?
        var completionTokens: Int?
        enum CodingKeys: String, CodingKey {
            case promptTokens = "prompt_tokens"
            case completionTokens = "completion_tokens"
        }
    }

    enum CodingKeys: String, CodingKey {
        case steps, model, provider, usage
        case elapsedMs = "elapsed_ms"
    }
}

/// 附件上传回执；attachment_id 随后在发消息时引用（24 小时未引用会被服务端回收）
nonisolated struct AgentAttachmentUpload: Decodable, Sendable {
    var attachmentId: String
    var name: String

    enum CodingKeys: String, CodingKey {
        case name
        case attachmentId = "attachment_id"
    }
}

// MARK: - 手写接口

nonisolated extension APIClient {
    /// 会话详情（完整轨迹回放）。`GET /sessions/{id}`
    func agentTranscript(sessionId: String) async throws -> AgentTranscript {
        try await send("GET", "/sessions/\(sessionId)")
    }

    /// 上传一张图片附件。`POST /sessions/attachments`（multipart，字段名 file）
    func agentUploadAttachment(data: Data, filename: String, mimeType: String) async throws -> AgentAttachmentUpload {
        try await upload("/sessions/attachments", file: (name: "file", filename: filename, mimeType: mimeType, data: data))
    }

    /// 会话内附件的取图地址（内容不可变，图片缓存可长期复用）
    func agentAttachmentURL(sessionId: String, attachmentId: String) -> URL {
        url("/sessions/\(sessionId)/attachments/\(attachmentId)")
    }
}
