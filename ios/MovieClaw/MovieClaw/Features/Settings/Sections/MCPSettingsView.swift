import SwiftUI

/// 设置 → MCP 服务（对应 Web `mcp-section.tsx` 的列表视图，设计见 docs/design/mcp-server.md §7）。
///
/// Web 是 4xl 宽的开发者控制台：列表 → 详情（三栏）→ 新建（两栏）都在同一块内容区里原地切换。
/// 手机上改成系统层级，信息与操作一个不少：
/// - **列表**（本页）：总开关（地址前缀）、「已配置 N 个端点」+ 新建、服务关闭提示、
///   端点行（状态点 / 名称 / 路径 / 服务标签 / 工具数 · 形态 · 最近调用），同 Web 窄屏卡片版式；
/// - **详情**：点行 push `SettingsBMCPEndpointDetail`（概览 / 工具 / 设置）；
/// - **新建**：弹层 `SettingsBMCPCreateSheet`，成功后原地换成令牌专屏，确认保存后自动推入新端点详情。
///
/// 状态由 `SettingsBMCPStore` 在列表与详情间共享，所有写操作后整份重拉 `GET /mcp/status`。
struct MCPSettingsView: View {
    @Environment(\.api) private var api
    @State private var store = SettingsBMCPStore()
    @State private var creating = false
    /// 当前推入的端点 id（点行或新建完成后赋值）
    @State private var openEndpoint: String?
    /// 新建弹层关闭后要推入的端点：等弹层完全收起再 push，避免转场打架
    @State private var pendingOpen: String?

    var body: some View {
        Group {
            if let status = store.status {
                list(status)
            } else if let error = store.error {
                ErrorState(message: error) { await store.load(api) }
            } else {
                // 首载：布局先占位（Web 骨架屏）
                ProgressView().controlSize(.large)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityIdentifier("loading")
            }
        }
        .appBackground()
        .task { await store.load(api) }
        .navigationDestination(item: $openEndpoint) { id in
            SettingsBMCPEndpointDetail(store: store, endpointId: id)
        }
        .sheet(isPresented: $creating, onDismiss: {
            if let pendingOpen {
                openEndpoint = pendingOpen
                self.pendingOpen = nil
            }
        }) {
            SettingsBMCPCreateSheet(store: store) { pendingOpen = $0 }
                .sheetFeedback()
        }
    }

    private func list(_ status: API.StatusView) -> some View {
        Form {
            if let error = store.error {
                Section {
                    SettingsBNotice(text: error, tone: .danger).accessibilityIdentifier("mcp-error")
                }
            }

            // 总开关：左边写清地址前缀，右边一个开关（与 Webhook / 消息推送同形态）
            Section {
                Toggle(isOn: Binding(
                    get: { status.enabled },
                    set: { enabled in
                        Task { _ = await store.run(api) { try await api.mcpToggle(body: .init(enabled: enabled)) } }
                    }
                )) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("启用 MCP 服务")
                        Text("\(status.baseUrl.isEmpty ? "（未配置外部地址）" : status.baseUrl)/mcp/<端点>")
                            .font(.caption.monospaced())
                            .foregroundStyle(Theme.textFaint)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                .disabled(store.busy)
                .accessibilityIdentifier("mcp-enabled")
            }

            Section {
                if !status.enabled && !status.endpoints.isEmpty {
                    SettingsBNotice(text: "服务已关闭，下面所有端点一律返回 404。配置与令牌都保留着，打开开关即恢复。", tone: .warn)
                        .accessibilityIdentifier("mcp-disabled-notice")
                }
                if status.endpoints.isEmpty {
                    VStack(spacing: 8) {
                        Text("还没有 MCP 端点").font(.subheadline.weight(.medium))
                        Text("端点是给 AI 客户端用的入口：建一个、勾选要开放的服务，Claude Code 或 Cursor 填上地址和令牌，就能直接查库存、搜资源、管订阅。每个端点的工具目录相互独立。")
                            .font(.caption)
                            .foregroundStyle(Theme.textMuted)
                            .multilineTextAlignment(.center)
                            .fixedSize(horizontal: false, vertical: true)
                        Text("点击右上角「新建端点」开始。").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 20)
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("mcp-empty")
                } else {
                    ForEach(status.endpoints, id: \.id) { endpoint in
                        row(endpoint)
                    }
                }
            } header: {
                HStack {
                    Text(status.endpoints.isEmpty ? "还没有 MCP 端点。" : "已配置 \(status.endpoints.count) 个端点。")
                        .textCase(nil)
                    Spacer()
                    Button {
                        store.error = nil
                        creating = true
                    } label: {
                        Label("新建端点", systemImage: "plus").font(.footnote.weight(.semibold))
                    }
                    .discoverProminentButton()
                    .disabled(store.busy)
                    .textCase(nil)
                    .accessibilityIdentifier("mcp-create")
                }
            }
        }
        .settingsBFormStyle()
        .refreshable { await store.load(api) }
    }

    /// 端点行（Web 窄屏卡片）：状态点 + 名称 / 路径 / 服务标签（最多 3）/ 工具数 · 形态 · 最近调用
    private func row(_ endpoint: API.EndpointView) -> some View {
        Button {
            openEndpoint = endpoint.id
        } label: {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        SettingsBMCPStatusDot(on: endpoint.enabled)
                        Text(endpoint.name).font(.body.weight(.medium)).foregroundStyle(Theme.text).lineLimit(1)
                    }
                    Text("/mcp/\(endpoint.slug)").font(.caption.monospaced()).foregroundStyle(Theme.textMuted)
                    SettingsBMCPServiceChips(services: endpoint.services, max: 3)
                    Text("\(endpoint.toolCount) 个工具 · \(SettingsBMCPFormat.mode(endpoint.expandTools)) · \(endpoint.lastUsedAt == nil ? "从未调用" : SettingsBMCPFormat.relative(endpoint.lastUsedAt))")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                }
                Spacer(minLength: 4)
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.textFaint).padding(.top, 4)
            }
            .contentShape(.rect)
            .opacity(endpoint.enabled ? 1 : 0.55)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("mcp-endpoint-\(endpoint.slug)")
    }
}
