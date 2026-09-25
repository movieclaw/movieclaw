import SwiftUI

/// 接入 / 编辑一个模型供应商实例（对应 Web `LlmProviderForm`，新增与编辑共用）。
///
/// 字段随所选供应商的预设动态变化（判据与 Web 完全一致）：
/// - **端点与 User-Agent** 只对「预设没有固定端点」的供应商开放（OpenAI 可配镜像、通用兼容端点必填）；
///   百炼 / DeepSeek / Kimi / GLM 这类官方固定渠道两者都不展示，提交时 UA 一律回传 null，
///   避免切换供应商后残留上一个自定义端点的 UA。
/// - **模型目录**：有内置目录的官方渠道以预设为准、不开放自定义；无目录的兼容端点必须自己补录
///   （可「借用」其它预设目录里的同名模型，参数原样复制；或展开参数子表单填一个自定义模型）。
///   切换供应商不删除已补录的条目，切回即恢复。
/// - **API Key** 出于安全后端不回传（接口也要求必填），编辑时同样要重新填写；输入默认遮罩、可切换显示。
///
/// 保存后由后端异步测试连接，列表页随后轮询状态，所以这里保存成功即关闭弹层（Web 同样不弹 Toast）。
/// 失败原因（后端中文）显示在表单顶部，弹层保持打开以便修改。
struct SettingsBLLMProviderForm: View {
    /// 被编辑的实例；nil 表示新增
    let provider: API.LlmProviderView?
    let presets: [API.LlmPresetView]
    /// 其它实例已占用的实例名（实例名全局唯一）
    let takenNames: [String]
    let onSaved: () -> Void

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var name: String
    @State private var providerType: String
    @State private var baseUrl: String
    @State private var userAgent: String
    @State private var advancedOpen: Bool
    @State private var apiKey = ""
    @State private var showApiKey = false
    /// 自定义模型目录（随配置持久化）：无内置目录的兼容端点靠它定义可用模型
    @State private var extraModels: [API.ModelInfo]
    /// 「自定义模型」参数子表单：只在用户选择添加自定义模型时展开
    @State private var addingCustom = false
    @State private var draft = SettingsBLLMModelDraft()
    @State private var busy = false
    @State private var error: String?

    init(provider: API.LlmProviderView?, presets: [API.LlmPresetView], takenNames: [String], onSaved: @escaping () -> Void) {
        self.provider = provider
        self.presets = presets
        self.takenNames = takenNames
        self.onSaved = onSaved
        _name = State(initialValue: provider?.name ?? "")
        _providerType = State(initialValue: provider?.providerType ?? "bailian")
        _baseUrl = State(initialValue: provider?.baseUrl ?? "")
        _userAgent = State(initialValue: provider?.userAgent ?? "")
        _advancedOpen = State(initialValue: provider?.userAgent != nil)
        _extraModels = State(initialValue: provider?.extraModels ?? [])
    }

    // MARK: - 派生状态（与 Web 同名变量一一对应）

    private var preset: API.LlmPresetView? { presets.first { $0.id == providerType } }
    private var catalog: [API.ModelInfo] { preset?.models ?? [] }
    private var isCustomEndpoint: Bool { catalog.isEmpty }
    private var extras: [API.ModelInfo] { isCustomEndpoint ? extraModels : [] }
    private var needBaseUrl: Bool { preset?.requiresBaseUrl ?? false }
    /// 端点没有预设默认值 = 用户自定义接入点，只有这类才需要端点与 User-Agent 输入
    private var canCustomizeEndpoint: Bool { preset?.baseUrl == nil }

    /// 无内置目录的端点：把其它预设目录的模型也纳入可添加（换端点时模型名与参数往往一致）
    private var borrowedGroups: [(label: String, models: [API.ModelInfo])] {
        guard isCustomEndpoint else { return [] }
        return presets
            .filter { $0.id != providerType && !$0.models.isEmpty }
            .map { p in (p.displayName, p.models.filter { m in !extras.contains { $0.id == m.id } }) }
            .filter { !$0.1.isEmpty }
    }

    private var trimmedBaseUrl: String { baseUrl.trimmingCharacters(in: .whitespaces) }
    private var trimmedUA: String { userAgent.trimmingCharacters(in: .whitespaces) }

    private var baseUrlValid: Bool {
        if trimmedBaseUrl.isEmpty { return !needBaseUrl }
        return trimmedBaseUrl.range(of: #"^https?://.+"#, options: .regularExpression) != nil
    }

    /// 请求头值只能是可打印 ASCII（与后端校验同一判据），空即用 SDK 默认
    private var userAgentValid: Bool {
        trimmedUA.isEmpty || trimmedUA.unicodeScalars.allSatisfy { (0x20...0x7E).contains($0.value) }
    }

    private var submitHint: String? {
        if name.contains("/") { return "实例名不能包含斜杠 /。" }
        if apiKey.trimmingCharacters(in: .whitespaces).isEmpty { return "请填写 API Key。" }
        if !baseUrlValid { return "请填写以 http:// 或 https:// 开头的完整 API 端点。" }
        if isCustomEndpoint && extras.isEmpty { return "请至少添加一个模型（接入后才有模型可用）。" }
        if !userAgentValid { return "User-Agent 包含不支持的字符，请检查高级设置。" }
        return nil
    }

    // MARK: - 界面

    var body: some View {
        NavigationStack {
            Form {
                if let error {
                    Section {
                        SettingsBNotice(text: error, tone: .danger)
                            .accessibilityIdentifier("llm-form-error")
                    }
                }
                providerSection
                connectionSection
                catalogSection
                if isCustomEndpoint && addingCustom {
                    customModelSection
                }
                if let submitHint {
                    Section {
                        Text(submitHint)
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                            .accessibilityIdentifier("llm-form-hint")
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .navigationTitle(provider.map { "编辑「\($0.name)」" } ?? "接入模型供应商")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消", systemImage: "xmark") { dismiss() }
                        .accessibilityIdentifier("llm-form-cancel")
                }
            }
            .safeAreaBar(edge: .bottom) {
                SubsPrimaryButton(
                    title: busy ? "保存中…" : "保存并测试连接",
                    busy: busy,
                    enabled: submitHint == nil,
                    identifier: "llm-form-save"
                ) {
                    Task { await submit() }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
            }
            .background(Theme.background.opacity(0.35))
        }
        .presentationBackground(.regularMaterial)
        .interactiveDismissDisabled(busy)
        .onChange(of: providerType) { _, _ in
            // 换供应商：端点 / UA / 高级设置 / 参数子表单都与上一家无关，一并清掉
            baseUrl = ""
            userAgent = ""
            advancedOpen = false
            addingCustom = false
        }
    }

    /// 供应商（下拉）+ 所选供应商的提示卡
    private var providerSection: some View {
        Section {
            Picker("供应商", selection: $providerType) {
                if presets.isEmpty {
                    Text("正在加载供应商…").tag(providerType)
                }
                ForEach(presets, id: \.id) { item in
                    Text(item.displayName).tag(item.id)
                }
            }
            .pickerStyle(.menu)
            .disabled(presets.isEmpty)
            .accessibilityIdentifier("llm-form-provider")
            if let preset {
                VStack(alignment: .leading, spacing: 2) {
                    Text(preset.displayName).font(.subheadline.weight(.semibold))
                    Text(SettingsBLLMFormat.providerHint(preset)).font(.caption).foregroundStyle(Theme.textFaint)
                }
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("llm-form-provider-hint")
            }
        } header: {
            Text("供应商")
        } footer: {
            Text("保存后系统会自动测试连接。")
        }
    }

    /// 实例名 / API 端点 / API Key / 高级设置（User-Agent）
    private var connectionSection: some View {
        Section {
            SettingsBTextField(
                label: "实例名（可选）",
                text: $name,
                placeholder: preset?.displayName ?? "如：官方 OpenAI / 家里的 vLLM",
                hint: "接入多家时用来区分：同一模型在多家都有时，对话框会以「模型（实例名）」标注。留空按供应商名保存。",
                identifier: "llm-form-name"
            )

            // 端点固定的官方渠道不展示端点输入；OpenAI 保留可选输入（代理/镜像），通用兼容端点必填
            if canCustomizeEndpoint {
                SettingsBTextField(
                    label: "API 端点" + (needBaseUrl ? " *" : "（可选）"),
                    text: $baseUrl,
                    placeholder: needBaseUrl ? "http://192.168.1.5:8000/v1" : "官方默认端点",
                    hint: needBaseUrl ? nil : "留空使用官方端点；使用代理或镜像时填写完整地址。",
                    mono: true,
                    keyboard: .URL,
                    identifier: "llm-form-base-url"
                )
            }

            apiKeyField

            // User-Agent 是排障用低频字段，默认折叠以缩短主流程
            if canCustomizeEndpoint {
                DisclosureGroup(isExpanded: $advancedOpen) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("User-Agent（可选）").font(.subheadline).foregroundStyle(Theme.textMuted)
                        // 占位符直接展示留空时实际发送的 SDK 自带 UA（后端按 SDK 版本现算）
                        TextField(preset?.defaultUserAgent ?? "留空使用 SDK 默认标识", text: $userAgent)
                            .font(.body.monospaced())
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .accessibilityIdentifier("llm-form-user-agent")
                        Text(userAgentValid
                             ? "仅当网关或 WAF 对请求标识有要求时填写，留空使用上方显示的 SDK 默认值。"
                             : "只能包含可打印的 ASCII 字符（不能含换行或中文）。")
                            .font(.caption)
                            .foregroundStyle(userAgentValid ? Theme.textFaint : Theme.danger)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } label: {
                    HStack {
                        Text("高级设置")
                        Spacer()
                        if !advancedOpen {
                            Text("User-Agent").font(.caption).foregroundStyle(Theme.textFaint)
                        }
                    }
                }
                .accessibilityIdentifier("llm-form-advanced")
            }
        }
    }

    /// API Key：默认遮罩，右侧「显示 / 隐藏」切换；不回显已保存的密钥
    private var apiKeyField: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("API Key *").font(.subheadline).foregroundStyle(Theme.textMuted)
            HStack {
                Group {
                    if showApiKey {
                        TextField(provider == nil ? "sk-…" : "出于安全，请重新填写", text: $apiKey)
                    } else {
                        SecureField(provider == nil ? "sk-…" : "出于安全，请重新填写", text: $apiKey)
                    }
                }
                .font(.body.monospaced())
                .textContentType(nil)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .accessibilityIdentifier("llm-form-api-key")
                Button(showApiKey ? "隐藏" : "显示") { showApiKey.toggle() }
                    .font(.subheadline.weight(.medium))
                    .buttonStyle(.borderless)
                    .accessibilityLabel(showApiKey ? "隐藏 API Key" : "显示 API Key")
                    .accessibilityIdentifier("llm-form-api-key-toggle")
            }
            if provider != nil {
                Text("已保存的密钥不会回显，修改其他配置时也需要重新填写。")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 2)
    }

    /// 模型目录：兼容端点列出已补录的模型 + 「添加模型」菜单；官方渠道只写一句预设说明
    @ViewBuilder
    private var catalogSection: some View {
        if isCustomEndpoint {
            Section {
                ForEach(extras, id: \.id) { m in
                    HStack(spacing: 12) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(m.id).font(.subheadline.weight(.medium)).lineLimit(1)
                            let detail = [SettingsBLLMFormat.specs(m), SettingsBLLMFormat.hints(m)]
                                .filter { !$0.isEmpty }.joined(separator: " · ")
                            Text(detail.isEmpty ? "参数以官方文档为准" : detail)
                                .font(.caption)
                                .foregroundStyle(Theme.textFaint)
                                .lineLimit(2)
                        }
                        Spacer(minLength: 8)
                        Button("移除") { extraModels.removeAll { $0.id == m.id } }
                            .font(.caption)
                            .buttonStyle(.borderless)
                            .foregroundStyle(Theme.textMuted)
                            .accessibilityIdentifier("llm-model-remove-\(m.id)")
                    }
                    .accessibilityIdentifier("llm-model-\(m.id)")
                }
                Menu {
                    // 其它预设目录的模型：选中即入列，参数复用
                    ForEach(borrowedGroups, id: \.label) { group in
                        Section("\(group.label) 目录（同名模型参数复用）") {
                            ForEach(group.models, id: \.id) { m in
                                let hints = SettingsBLLMFormat.hints(m)
                                Button(m.id + (hints.isEmpty ? "" : "（\(hints)）")) { borrow(m) }
                            }
                        }
                    }
                    Button("自定义模型（填写参数）…") { addingCustom = true }
                } label: {
                    Label("添加模型…", systemImage: "plus")
                }
                .accessibilityIdentifier("llm-form-add-model")
            } header: {
                Text("模型目录 *")
            } footer: {
                Text("兼容端点没有内置目录，请把端点上部署的模型添加进来（含参数）。接入后这些模型可在 AI 设定与对话框里选用；连接测试用第一个。")
            }
        } else if let preset {
            Section {
                Text("模型目录以「\(preset.displayName)」预设为准，共 \(catalog.count) 个模型，接入后可在 AI 设定与对话框里选用；连接测试用目录里第一个。")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("llm-form-catalog-note")
            }
        }
    }

    /// 自定义模型参数子表单：这些参数是 agent 做上下文 / 思考预算决策的依据，必填项带 *
    private var customModelSection: some View {
        Section {
            SettingsBTextField(label: "模型 id *", text: $draft.id, placeholder: "如：my-vllm-model", mono: true, identifier: "llm-custom-id")
            SettingsBNumberField(label: "上下文长度 *", text: $draft.contextWindow, placeholder: "131072", identifier: "llm-custom-context")
            SettingsBNumberField(label: "最大输出 *", text: $draft.maxOutput, placeholder: "8192", identifier: "llm-custom-max-output")
            SettingsBNumberField(label: "最大输入（可选）", text: $draft.maxInput, placeholder: "不单独限制可留空", identifier: "llm-custom-max-input")

            Toggle("支持工具调用", isOn: Binding(
                get: { draft.supportsTools },
                set: { draft.supportsTools = $0; draft.parallel = $0 && draft.parallel }
            ))
            .accessibilityIdentifier("llm-custom-tools")
            Toggle("支持并发工具调用", isOn: $draft.parallel)
                .disabled(!draft.supportsTools)
                .accessibilityIdentifier("llm-custom-parallel")
            Toggle("输出思考内容", isOn: Binding(
                get: { draft.thinking },
                set: { draft.thinking = $0; if !$0 { draft.thinkingKind = .none } }
            ))
            .accessibilityIdentifier("llm-custom-thinking")
            Toggle("支持图片输入", isOn: $draft.vision).accessibilityIdentifier("llm-custom-vision")
            Toggle("支持视频输入", isOn: $draft.video).accessibilityIdentifier("llm-custom-video")

            if draft.thinking {
                VStack(alignment: .leading, spacing: 4) {
                    SettingsBNumberField(label: "思考预算上限 *", text: $draft.thinkingBudget, placeholder: "如：81920", identifier: "llm-custom-thinking-budget")
                    Text("思维链可用的最大 token 数（thinking_budget 上限），超配供应商会直接报错。")
                        .font(.caption).foregroundStyle(Theme.textFaint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 6) {
                    Picker("思考强度控制", selection: $draft.thinkingKind) {
                        ForEach(SettingsBLLMModelDraft.ThinkingKind.allCases) { kind in
                            Text(kind.label).tag(kind)
                        }
                    }
                    .pickerStyle(.menu)
                    .accessibilityIdentifier("llm-custom-thinking-kind")
                    Text("按端点方言选择；声明后会话输入框出现思考档位选择器，档位只从声明的菜单里选（不做就近换算）。")
                        .font(.caption).foregroundStyle(Theme.textFaint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if draft.thinkingKind == .effort {
                    SettingsBFlow {
                        ForEach(SettingsBLLMModelDraft.declarableLevels, id: \.self) { level in
                            SettingsBSelectChip(
                                title: SettingsBLLMModelDraft.levelLabels[level] ?? level,
                                selected: draft.thinkingLevels.contains(level),
                                identifier: "llm-custom-level-\(level)"
                            ) {
                                draft.toggleLevel(level)
                            }
                        }
                    }
                    .padding(.vertical, 4)
                }
                if draft.thinkingKind == .effort || draft.thinkingKind == .budget {
                    VStack(alignment: .leading, spacing: 4) {
                        Toggle("支持关闭思考", isOn: $draft.supportsOff)
                            .accessibilityIdentifier("llm-custom-supports-off")
                        Text("仅当端点有真正的关闭参数时勾选（如 enable_thinking=false）；勾选后档位菜单多出「关」。")
                            .font(.caption).foregroundStyle(Theme.textFaint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            if !draft.isValid {
                Text(draft.hint).font(.caption).foregroundStyle(Theme.textFaint)
            }
            HStack(spacing: 10) {
                Button {
                    addCustomModel()
                } label: {
                    Text("添加到目录").font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity)
                }
                .discoverProminentButton()
                .disabled(!draft.isValid)
                .accessibilityIdentifier("llm-custom-add")
                Button {
                    draft = SettingsBLLMModelDraft()
                    addingCustom = false
                } label: {
                    Text("取消").font(.subheadline.weight(.medium)).frame(maxWidth: .infinity)
                }
                .buttonStyle(.glass)
                .accessibilityIdentifier("llm-custom-cancel")
            }
        } header: {
            Text("自定义模型参数")
        } footer: {
            Text("按端点实际部署的模型规格填写，添加后计入本实例的模型目录")
        }
    }

    // MARK: - 动作

    private func borrow(_ model: API.ModelInfo) {
        guard !extraModels.contains(where: { $0.id == model.id }) else { return }
        extraModels.append(model)
    }

    /// 把参数子表单里的自定义模型加进目录（同 id 覆盖旧条目：改参数重存是常见操作）
    private func addCustomModel() {
        let custom = draft.build()
        extraModels = extraModels.filter { $0.id != custom.id } + [custom]
        draft = SettingsBLLMModelDraft()
        addingCustom = false
    }

    /// 留空时按供应商显示名保存；同类型第二家会撞唯一实例名，自动加序号（「OpenAI 2」）
    private func fallbackName() -> String {
        let base = preset?.displayName ?? providerType
        var candidate = base
        var i = 2
        while takenNames.contains(candidate) {
            candidate = "\(base) \(i)"
            i += 1
        }
        return candidate
    }

    private func submit() async {
        guard submitHint == nil else { return }
        busy = true
        error = nil
        defer { busy = false }
        let trimmedName = name.trimmingCharacters(in: .whitespaces)
        let payload = API.LlmProviderPayload(
            name: trimmedName.isEmpty ? fallbackName() : trimmedName,
            providerType: providerType,
            baseUrl: trimmedBaseUrl.isEmpty ? nil : trimmedBaseUrl,
            // 端点固定的官方渠道不开放 UA 配置，一律回传 null
            userAgent: canCustomizeEndpoint && !trimmedUA.isEmpty ? trimmedUA : nil,
            apiKey: apiKey.trimmingCharacters(in: .whitespaces),
            // 连接测试模型由服务端取目录第一个；官方渠道不回传自定义目录
            defaultModel: nil,
            extraModels: extras.map(\.settingsBLLMInput)
        )
        do {
            if let provider {
                _ = try await api.llmProvidersUpdate(providerId: provider.id, body: payload)
            } else {
                _ = try await api.llmProvidersCreate(body: payload)
                LLMCapabilityProbe.shared.invalidate()
            }
            onSaved()
            dismiss()
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }
}
