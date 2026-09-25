import SwiftUI

/// 设置 → AI 设定（Web ai-settings-section.tsx）：什么场景用哪个模型。
///
/// 与「模型接入」分开：接入回答「怎么连上」，这里回答「智能体 / 字幕处理默认用哪个模型」。
/// 选项是所有已接入实例的模型清单（`GET /llm/models`）；服务端保证接入过供应商就一定有默认值，
/// 所以这里显示的永远是真实存的值。一个都没接入时给空态，引导去「模型接入」。
/// 选择即保存（`PUT /llm/defaults`），乐观更新、失败回滚；另一项若已失效（不在清单里）不能原样回传，
/// 传空让服务端按推荐补齐。
struct AIDefaultsSettingsView: View {
    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @Environment(Feedback.self) private var feedback

    @State private var defaults: API.LlmDefaultsView?
    @State private var options: [API.LlmModelOptionView] = []
    @State private var error: String?
    @State private var saving: Purpose?

    enum Purpose: String, CaseIterable {
        case agent, subtitle

        var label: String { self == .agent ? "智能体默认模型" : "字幕处理默认模型" }
        var desc: String {
            self == .agent
                ? "对话框未选模型时、微信 / Telegram / Discord 对话、命令行不带 --model 时使用"
                : "字幕翻译与生成任务使用；任务创建时固定，改动不影响已开始的任务"
        }

        func value(_ d: API.LlmDefaultsView) -> String? { self == .agent ? d.agentModel : d.subtitleModel }
        func effective(_ d: API.LlmDefaultsView) -> String? { self == .agent ? d.effectiveAgentModel : d.effectiveSubtitleModel }
    }

    var body: some View {
        List {
            if defaults != nil, options.isEmpty {
                emptyState
            } else {
                Section {
                    (Text("为不同场景各选一个默认模型。可选项来自") + Text("「模型接入」").foregroundStyle(Theme.accent)
                        + Text("里所有已接入供应商的模型目录；首次接入时已自动设为该供应商目录里的第一个模型，可随时更改。"))
                        .font(.subheadline).foregroundStyle(Theme.textMuted)
                        .onTapGesture { router.push(.settingsSection(.llm)) }
                        .listRowBackground(Color.clear)
                }
                if let error { Section { SettingsNotice(text: error) } }
                if let defaults {
                    ForEach(Purpose.allCases, id: \.self) { purpose in
                        purposeSection(purpose, defaults)
                    }
                } else if error == nil {
                    Section { SettingsLoadingRow() }
                }
            }
        }
        .appBackground()
        .task { await load() }
    }

    private var emptyState: some View {
        Section {
            VStack(spacing: 10) {
                Image(systemName: "sparkles").font(.title).foregroundStyle(Theme.textMuted)
                Text("还没有可选的模型").font(.body.weight(.medium))
                Text("先在「模型接入」接入至少一家供应商。接入后这里会自动把智能体和字幕处理的默认模型设为该供应商目录里的第一个模型，你可以随时改成别的。")
                    .font(.subheadline).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
                Button("去接入模型供应商") { router.push(.settingsSection(.llm)) }
                    .buttonStyle(.glassProminent)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 16)
        }
    }

    private func purposeSection(_ purpose: Purpose, _ defaults: API.LlmDefaultsView) -> some View {
        let value = purpose.value(defaults)
        // 正常情况下一定有值且在清单里；只有预设目录漂移才会失效，此时提示并展示实际生效的兜底值
        let stale = value == nil || !options.contains { $0.ref == value }
        return Section {
            Picker(selection: Binding(
                get: { stale ? "" : (value ?? "") },
                set: { next in Task { await save(purpose, next.isEmpty ? nil : next) } }
            )) {
                if stale { Text("请重新选择…").tag("") }
                ForEach(options, id: \.ref) { option in
                    Text(option.label + (option.thinkingLevels.isEmpty ? "" : "（思考档位可控）")).tag(option.ref)
                }
            } label: {
                SettingsRowText(title: purpose.label, detail: purpose.desc)
            }
            .pickerStyle(.navigationLink)
            .disabled(saving != nil)
            .accessibilityIdentifier("ai-\(purpose.rawValue)-model")
            if stale {
                Text("原设定的「\(value ?? "（空）")」已不在模型清单里，当前自动使用\(label(of: purpose.effective(defaults)) ?? "无")；请重新选择。")
                    .font(.caption).foregroundStyle(Theme.warning)
            }
        }
    }

    private func label(of ref: String?) -> String? {
        options.first { $0.ref == ref }?.label ?? ref
    }

    private func load() async {
        do {
            async let d = api.llmDefaultsShow()
            async let o = api.llmModels()
            let (loadedDefaults, loadedOptions) = try await (d, o)
            options = loadedOptions
            defaults = loadedDefaults
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func save(_ purpose: Purpose, _ value: String?) async {
        guard let previous = defaults else { return }
        var optimistic = previous
        if purpose == .agent { optimistic.agentModel = value } else { optimistic.subtitleModel = value }
        defaults = optimistic
        saving = purpose
        error = nil
        defer { saving = nil }
        /// 另一项若已失效，传空让服务端按推荐补齐（原样回传会被整体拒绝）
        func sibling(_ ref: String?) -> String? {
            guard let ref, options.contains(where: { $0.ref == ref }) else { return nil }
            return ref
        }
        do {
            defaults = try await api.llmDefaultsUpdate(body: .init(
                agentModel: purpose == .agent ? value : sibling(previous.agentModel),
                subtitleModel: purpose == .subtitle ? value : sibling(previous.subtitleModel)
            ))
            feedback.success("AI 设定已保存")
        } catch {
            defaults = previous
            self.error = error.localizedDescription
        }
    }
}
