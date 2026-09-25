import Foundation

/// 技能显式调用的文本规则（对应 Web `lib/agent-skills.ts`，两个正则都是后端 movieclaw_agent/skills.py 的镜像）。
///
/// - 输入框里的 `/skill:名字` 占位符：行首或空白后出现，发送时由服务端展开成 `<skill>` 块；
/// - 轨迹回放时 user 消息已是展开后的形态，先还原成 token 形态（`toTokenForm`），
///   气泡再拆成 chip + 用户原文（`parseTokens`）；「改写重问」直接编辑 token 形态，重发时服务端重新展开。
enum AgentSkillText {
    /// 组装一个插入输入框的占位符（尾随空格便于继续输入）
    static func token(_ name: String) -> String { "/skill:\(name) " }

    private static let tokenRegex = try! NSRegularExpression(pattern: #"(?:^|(?<=\s))/skill:([A-Za-z0-9._-]+)"#)
    private static let blockRegex = try! NSRegularExpression(pattern: #"^<skill name="([^"]*)" location="[^"]*">\n[\s\S]*?\n</skill>\n*"#)

    /// 从文本中拆出技能名（首现序去重）与剩余原文。
    /// `allow`：已知技能名（小写）；传入时只拆名单内的 token——服务端只展开已知技能，
    /// 拼错的 token 原样留给模型解释，气泡不能把它画成「已调用」的 chip。
    static func parseTokens(_ text: String, allow: Set<String>? = nil) -> (names: [String], text: String) {
        var names: [String] = []
        var seen = Set<String>()
        let lines = text.components(separatedBy: "\n").map { line -> String in
            let ns = line as NSString
            var out = ""
            var cursor = 0
            var touched = false
            for match in tokenRegex.matches(in: line, range: NSRange(location: 0, length: ns.length)) {
                let name = ns.substring(with: match.range(at: 1))
                let key = name.lowercased()
                if let allow, !allow.contains(key) { continue } // 未知技能：保留字面文本
                touched = true
                if seen.insert(key).inserted { names.append(name) }
                out += ns.substring(with: NSRange(location: cursor, length: match.range.location - cursor))
                cursor = match.range.location + match.range.length
            }
            guard touched else { return line }
            out += ns.substring(from: cursor)
            return out.replacingOccurrences(of: #"[ \t]+"#, with: " ", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces)
        }
        let kept = lines.enumerated().filter { i, line in !line.isEmpty || (i > 0 && i < lines.count - 1) }.map(\.element)
        return (names, kept.joined(separator: "\n"))
    }

    /// 服务端已展开的 user 消息 → token 形态 `/skill:a /skill:b 用户原文`；非展开消息原样返回
    static func toTokenForm(_ text: String) -> String {
        var names: [String] = []
        var rest = text
        while let match = blockRegex.firstMatch(in: rest, range: NSRange(location: 0, length: (rest as NSString).length)) {
            let ns = rest as NSString
            names.append(ns.substring(with: match.range(at: 1)))
            rest = ns.substring(from: match.range.location + match.range.length)
        }
        guard !names.isEmpty else { return text }
        return names.map(token).joined() + rest
    }

    /// 输入框末尾的「/查询」触发技能快选：行首或空白后敲「/」即开菜单，后续字符收窄匹配。
    /// 返回查询串（不含 `/`；`skill:` 前缀视为手敲完整占位符，按名字部分过滤）与待替换范围。
    static func slashQuery(in text: String) -> (query: String, range: Range<String.Index>)? {
        guard let match = text.range(of: #"(^|\s)/[A-Za-z0-9._:-]{0,64}$"#, options: .regularExpression) else { return nil }
        var start = match.lowerBound
        if text[start] != "/" { start = text.index(after: start) }
        var query = String(text[text.index(after: start)...]).lowercased()
        if query.hasPrefix("skill:") { query = String(query.dropFirst(6)) }
        if query.contains(":") { return nil }
        return (query, start ..< text.endIndex)
    }
}

/// 模型清单、技能清单与未接入模型的门禁（对应 Web `llm-gate.tsx` / `llm-thinking.ts` / `skill-names.ts`）。
///
/// 会话页与新任务页共享同一份模型清单：模块级缓存，一分钟内不重复请求（设置页改了模型后最迟一分钟生效）；
/// 失败不缓存，下次进入重试。技能清单在加号菜单与「/」快选每次打开时现拉（服务端改技能即生效），
/// 气泡判断「哪些 token 是已知技能」则用缓存的名单。
@MainActor
enum AgentCatalog {
    private static var models: (list: [API.LlmModelOptionView], at: Date)?
    private static var skillNames: Set<String>?

    /// 对话框可选的模型清单；加载失败返回空（隐藏选择器）
    static func modelOptions(api: APIClient) async -> [API.LlmModelOptionView] {
        if let models, Date.now.timeIntervalSince(models.at) < 60 { return models.list }
        guard let list = try? await api.llmModels() else { return [] }
        models = (list, .now)
        return list
    }

    /// 已知技能名（小写）；nil = 还没拿到（调用方视为「暂不过滤」）
    static func knownSkills(api: APIClient) async -> Set<String>? {
        if let skillNames { return skillNames }
        guard let list = try? await api.skillsList() else { return nil }
        let names = Set(list.map { $0.name.lowercased() })
        skillNames = names
        return names
    }

    /// 门禁判定与后端 acquire_llm_router 对齐：只看是否至少接入了一个实例。
    /// 返回 false = 明确未配置；探测失败返回 nil（不锁定，交给提交时的服务端错误兜底）
    static func llmConfigured(api: APIClient) async -> Bool? {
        guard let providers = try? await api.llmProvidersList() else { return nil }
        return !providers.isEmpty
    }

    /// 选择器当前生效的选项：显式引用命中的项，否则全局默认项
    static func resolve(_ options: [API.LlmModelOptionView], ref: String?) -> API.LlmModelOptionView? {
        if let ref, let hit = options.first(where: { $0.ref == ref }) { return hit }
        return options.first(where: \.isDefault) ?? options.first
    }
}

/// 对话框「模型 / 思维链」最近一次的选择（本机记忆，对应 Web `lib/composer-prefs.ts`）。
///
/// 会话里的选择随消息写进轨迹、由服务端沿用；新任务页没有历史可沿用，以这里记下的上次选择为起点。
/// 记忆不是事实源：读回来要对着模型清单校验，失效的直接丢弃。
struct AgentComposerPrefs: Equatable, Codable {
    /// 模型引用；nil = 默认模型
    var model: String?
    /// 思维链档位；nil = 模型默认
    var thinking: String?

    private static let key = "movieclaw.composer.choice"

    static func load() -> AgentComposerPrefs {
        guard let data = UserDefaults.standard.data(forKey: key),
              let prefs = try? JSONDecoder().decode(AgentComposerPrefs.self, from: data) else { return .init() }
        return prefs
    }

    func save() {
        if model == nil, thinking == nil {
            UserDefaults.standard.removeObject(forKey: Self.key)
        } else if let data = try? JSONEncoder().encode(self) {
            UserDefaults.standard.set(data, forKey: Self.key)
        }
    }

    /// 对着已加载的清单校验：模型不在清单里 → 模型与档位一起丢弃；档位不在生效模型的菜单里 → 只丢档位
    func reconciled(with options: [API.LlmModelOptionView]) -> AgentComposerPrefs {
        guard !options.isEmpty else { return self }
        let effective: API.LlmModelOptionView
        if let model {
            guard let hit = options.first(where: { $0.ref == model }) else { return .init() }
            effective = hit
        } else {
            guard let fallback = options.first(where: \.isDefault) ?? options.first else { return self }
            effective = fallback
        }
        let thinking = thinking.flatMap { effective.thinkingLevels.contains($0) ? $0 : nil }
        return AgentComposerPrefs(model: model, thinking: thinking)
    }
}

/// 思维链强度控件的纯逻辑（对应 Web `lib/thinking-level-control.ts`）。
///
/// 服务端只下发一个字符串菜单（thinking_levels），前端按菜单形状决定控件长相：
/// 空 → 不渲染；一档 → 两格分段（默认 / 该档）；两档及以上 → 横向离散滑杆（更快 ↔ 更聪明）。
/// 「默认」= 不发参数、用模型自身行为，不是强度轴上的一点：滑杆上不占刻度，靠「恢复默认」回去。
enum AgentThinking {
    static let order = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
    static let labels: [String: String] = [
        "off": "关", "minimal": "最少", "low": "低", "medium": "中", "high": "高", "xhigh": "超高", "max": "最高",
    ]

    enum Shape { case hidden, toggle, slider }

    /// 服务端菜单 → 按词汇表排序的刻度（词汇表外的值丢弃）
    static func stops(_ levels: [String]) -> [String] { order.filter(levels.contains) }

    static func shape(_ stops: [String]) -> Shape {
        stops.isEmpty ? .hidden : stops.count == 1 ? .toggle : .slider
    }

    static func label(_ value: String?) -> String {
        guard let value else { return "默认" }
        return labels[value] ?? value
    }

    struct ListItem: Identifiable {
        var level: String?
        var label: String
        var description: String
        var id: String { level ?? "default" }
    }

    /// 分段形态的格子：只有「关」的模型（kimi-k2.6、glm-5.x）写成「开启（模型默认）/ 关闭」
    static func listItems(_ stops: [String]) -> [ListItem] {
        if stops == ["off"] {
            return [
                ListItem(level: nil, label: "开启（模型默认）", description: "不发送思考参数，沿用模型自身的默认行为"),
                ListItem(level: "off", label: "关闭", description: "发送关闭指令，不再输出思考过程，响应更快"),
            ]
        }
        return [ListItem(level: nil, label: "默认", description: "不发送思考参数，由模型自行决定强度")]
            + stops.map { ListItem(level: $0, label: label($0), description: "按「\(label($0))」强度思考") }
    }
}
