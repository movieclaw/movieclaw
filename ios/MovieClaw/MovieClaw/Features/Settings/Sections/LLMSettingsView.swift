import SwiftUI

/// 设置 → 模型接入（对应 Web `llm-config-section.tsx`）。
///
/// 这一页只回答「怎么连上」：可接入多个供应商实例（官方 + 中转 + 自建可并存），
/// 每个实例一个 Section（状态卡片，见 `SettingsBLLMProviderCard`）；
/// 哪个场景用哪个模型不在这里配，见「AI 设定」。
///
/// 保存后后端异步用目录第一个模型做一次最小对话验证，所以任一实例处于待测试 / 测试中时
/// 每 2 秒刷新列表直到落定（同 Web `useVisiblePolling(…, 2000)`），空闲时轮询回调不发请求。
/// 新建 / 编辑在 Web 上是原地把列表换成表单；手机上改为弹层（`SettingsBLLMProviderForm`），
/// 字段、校验、提示文案一致。
struct LLMSettingsView: View {
    /// 表单打开参数：provider 为 nil = 新建
    private struct SettingsBLLMEditing: Identifiable {
        let id = UUID()
        var provider: API.LlmProviderView?
    }

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var providers: [API.LlmProviderView]?
    @State private var presets: [API.LlmPresetView] = []
    @State private var error: String?
    @State private var editing: SettingsBLLMEditing?
    /// 任一卡片操作进行中：禁用全部卡片按钮（同 Web busy）
    @State private var busy = false

    private var inProgress: Bool {
        providers?.contains { SettingsBLLMStatus.inProgress($0.status) } ?? false
    }

    var body: some View {
        Form {
            if let error {
                Section {
                    SettingsBNotice(text: error, tone: .danger)
                        .accessibilityIdentifier("llm-error")
                }
            }
            Section {
                SettingsBIntro(text: introText)
            }
            if let providers {
                if providers.isEmpty {
                    emptySection
                } else {
                    ForEach(providers, id: \.id) { provider in
                        SettingsBLLMProviderCard(
                            provider: provider,
                            preset: presets.first { $0.id == provider.providerType },
                            busy: busy,
                            onEdit: { editing = SettingsBLLMEditing(provider: provider) },
                            onReverify: { await reverify(provider) },
                            onDelete: { await remove(provider) }
                        )
                    }
                    Section {
                        Button {
                            editing = SettingsBLLMEditing()
                        } label: {
                            Text("＋ 接入另一家供应商").frame(maxWidth: .infinity)
                        }
                        .accessibilityIdentifier("llm-create")
                    }
                }
            } else {
                Section {
                    ProgressView().frame(maxWidth: .infinity).accessibilityIdentifier("loading")
                }
            }
        }
        .settingsBFormStyle()
        .task { await initialLoad() }
        // 只有实例处于中间态时才真的请求；间隔每轮重新取值
        .polling(every: inProgress ? 2 : 10) {
            guard inProgress else { return }
            // 轮询失败静默重试，不打断页面
            if let list = try? await api.llmProvidersList() { providers = list }
        }
        .refreshable { await load() }
        .sheet(item: $editing) { target in
            SettingsBLLMProviderForm(
                provider: target.provider,
                presets: presets,
                // 其它实例已占用的名字：留空按供应商名保存时据此加序号，避免撞唯一名
                takenNames: (providers ?? []).filter { $0.id != target.provider?.id }.map(\.name)
            ) {
                Task { await load() }
            }
            .sheetFeedback()
        }
    }

    private var introText: String {
        guard let providers else { return "加载中…" }
        return providers.isEmpty
            ? "接入一个或多个大语言模型供应商，AI 能力（对话助手、字幕处理、智能识别等）将由它们驱动。"
            : "接入后该供应商目录里的全部模型都可在对话框里选用；各场景默认用哪个模型，在「AI 设定」里配置。"
    }

    /// 空态：一个都没接入
    private var emptySection: some View {
        Section {
            VStack(spacing: 12) {
                Image(systemName: "sparkles")
                    .font(.title2)
                    .frame(width: 48, height: 48)
                    .background(Color.white.opacity(0.07), in: .rect(cornerRadius: 14))
                Text("还没有接入模型供应商").font(.body.weight(.medium))
                Text("支持 OpenAI、阿里云百炼，以及任何 OpenAI 兼容端点（如自建 vLLM / Ollama）。")
                    .font(.footnote)
                    .foregroundStyle(Theme.textMuted)
                    .multilineTextAlignment(.center)
                Button {
                    editing = SettingsBLLMEditing()
                } label: {
                    Text("接入模型供应商").font(.body.weight(.semibold)).frame(maxWidth: .infinity)
                }
                .discoverProminentButton()
                .accessibilityIdentifier("llm-create")
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 16)
        }
    }

    // MARK: - 数据

    private func initialLoad() async {
        // 预设目录是静态数据，进分区拉一次即可
        async let presetsTask: Void = loadPresets()
        await load()
        await presetsTask
    }

    private func loadPresets() async {
        do {
            presets = try await api.llmPresets()
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func load() async {
        do {
            providers = try await api.llmProvidersList()
            error = nil
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
            if providers == nil { providers = [] }
        }
    }

    private func reverify(_ provider: API.LlmProviderView) async {
        busy = true
        defer { busy = false }
        do {
            _ = try await api.llmProvidersVerify(providerId: provider.id)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func remove(_ provider: API.LlmProviderView) async {
        guard await feedback.confirm(
            "删除「\(provider.name)」？",
            message: "它目录里的模型将不可再选；AI 设定中指向它的默认模型会自动兜底到其它已接入的供应商。",
            confirmTitle: "删除",
            destructive: true
        ) else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await api.llmProvidersDelete(providerId: provider.id)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
