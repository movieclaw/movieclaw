import SwiftUI

/// 一个已接入的模型供应商实例（对应 Web `ProviderCard`）：一个 Section 三段——
/// 1. 名称 + 状态胶囊，副标题「供应商 · N 个自定义模型 · 上次检查 X」（失败时换成失败原因）；
/// 2. 连接信息：API 端点 / 连接测试模型 / User-Agent（仅用户覆盖过才显示）；
/// 3. 操作行三等分：编辑配置 / 重新测试 / 删除——独占一行，避免窄屏与名称、状态互相挤压。
struct SettingsBLLMProviderCard: View {
    let provider: API.LlmProviderView
    let preset: API.LlmPresetView?
    let busy: Bool
    let onEdit: () -> Void
    let onReverify: () async -> Void
    let onDelete: () async -> Void

    var body: some View {
        Section {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(provider.name)
                        .font(.body.weight(.semibold))
                        .lineLimit(1)
                    SettingsBLLMStatusPill(status: provider.status)
                }
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(failed ? Theme.danger.mix(with: .white, by: 0.3) : Theme.textFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, 2)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("llm-provider-\(provider.id)")

            SettingsBValueRow(label: "API 端点", value: provider.baseUrl ?? preset?.baseUrl ?? "官方默认", mono: true)
            SettingsBValueRow(label: "连接测试模型", value: provider.defaultModel, mono: true)
            // 仅在用户覆盖过 UA 时展示——没配的用户不需要知道有这回事
            if let ua = provider.userAgent {
                SettingsBValueRow(label: "User-Agent", value: ua, mono: true)
            }

            HStack(spacing: 8) {
                Button {
                    onEdit()
                } label: {
                    Text("编辑配置").lineLimit(1).frame(maxWidth: .infinity)
                }
                .accessibilityIdentifier("llm-edit-\(provider.id)")

                SettingsBAsyncButton(action: onReverify) {
                    Text("重新测试").lineLimit(1).frame(maxWidth: .infinity)
                }
                .disabled(busy || SettingsBLLMStatus.inProgress(provider.status))
                .accessibilityIdentifier("llm-reverify-\(provider.id)")

                SettingsBAsyncButton(role: .destructive, action: onDelete) {
                    Text("删除").lineLimit(1).frame(maxWidth: .infinity)
                }
                .foregroundStyle(Theme.danger)
                .disabled(busy)
                .accessibilityIdentifier("llm-delete-\(provider.id)")
            }
            .font(.subheadline.weight(.medium))
            .buttonStyle(.glass)
        }
    }

    private var failed: Bool { provider.status == "failed" }

    /// 失败时直接写失败原因；否则「供应商 · N 个自定义模型 · 上次检查 X」
    private var subtitle: String {
        if failed, let reason = provider.lastError { return reason }
        let checked = Formatters.relative(provider.lastCheckedAt)
        return [
            preset?.displayName ?? provider.providerType,
            // 自定义端点的目录是用户补录的，条数比测试模型更有信息量
            provider.extraModels.isEmpty ? nil : "\(provider.extraModels.count) 个自定义模型",
            "上次检查 \(checked.isEmpty ? "从未" : checked)",
        ].compactMap { $0 }.joined(separator: " · ")
    }
}

/// 状态胶囊：圆点 + 文案（已连接 / 测试中 / 待测试 / 连接失败）
struct SettingsBLLMStatusPill: View {
    let status: String

    var body: some View {
        let tone = SettingsBLLMStatus.tone(status)
        HStack(spacing: 5) {
            SettingsBDot(tone: tone, size: 6)
            Text(SettingsBLLMStatus.label(status)).font(.caption.weight(.medium))
        }
        .foregroundStyle(tone == .neutral ? Theme.textMuted : tone.color)
        .padding(.horizontal, 8)
        .padding(.vertical, 2)
        .background((tone == .neutral ? Color.white : tone.color).opacity(0.12), in: .capsule)
        .fixedSize()
        .accessibilityIdentifier("llm-status")
    }
}
