import SwiftUI

/// 端点详情（对应 Web `mcp/endpoint-detail.tsx`）：一个端点的全部真相，按「读 → 用 → 改 → 删」分三栏。
///
/// Web 是 `?endpoint=slug&tab=` 的原地视图切换；手机上改为从列表 push 进来的独立页面，
/// 顶部分段控件切「概览 / 工具 N / 设置」，信息与操作一个不少：
/// - **概览**：启停开关、端点地址（可复制，未配外部地址时警告）、认证请求头、连通性自检、
///   服务 / 工具定义体积 / 令牌提示 + 轮换 / 最近调用、已失效服务提示；
/// - **工具**：工具目录（`SettingsBMCPToolCatalog`），数据来自工具面预览；
/// - **设置**：与新建共用的字段区（地址标识只读）+ 保存；底部危险区打字确认后删除。
///
/// 端点数据不在本页持有副本，每次都按 id 从共享 `SettingsBMCPStore` 现取——写操作后整份重拉，
/// 返回列表时两边一致。轮换令牌后弹出令牌专屏（与新建同一个），确认保存后回到概览。
struct SettingsBMCPEndpointDetail: View {
    let store: SettingsBMCPStore
    let endpointId: String

    enum Tab: String, CaseIterable, Identifiable {
        case overview = "概览", tools = "工具", settings = "设置"
        var id: String { rawValue }

        /// Web 地址栏 `?tab=` 的取值（overview / tools / settings）
        init?(query: String?) {
            switch query {
            case "overview": self = .overview
            case "tools": self = .tools
            case "settings": self = .settings
            default: return nil
            }
        }
    }

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss
    @State private var tab: Tab
    @State private var preview: API.PreviewView?
    @State private var check: API.SelfCheckView?
    @State private var checking = false
    @State private var draft: SettingsBMCPDraft?
    @State private var confirmText = ""
    @State private var issued: SettingsBMCPIssued?

    /// - Parameter initialTab: 深链 `?endpoint=&tab=` 直达的栏目；缺省概览
    init(store: SettingsBMCPStore, endpointId: String, initialTab: Tab = .overview) {
        self.store = store
        self.endpointId = endpointId
        _tab = State(initialValue: initialTab)
    }

    var body: some View {
        Group {
            if let endpoint = store.endpoint(id: endpointId) {
                content(endpoint)
                    .task(id: PreviewKey(services: endpoint.services, expand: endpoint.expandTools)) {
                        await loadPreview(endpoint)
                    }
            } else {
                EmptyState(systemImage: "questionmark.folder", title: "端点不存在", message: "它可能已被删除")
            }
        }
        .navigationTitle(store.endpoint(id: endpointId)?.name ?? "端点")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $issued) { item in
            SettingsBMCPTokenIssuedView(
                name: item.created.endpoint.name,
                url: store.fullURL(item.created.endpoint),
                token: item.created.token
            ) {
                issued = nil
                tab = .overview
            }
            .sheetFeedback()
        }
    }

    private struct PreviewKey: Equatable {
        var services: [String]
        var expand: Bool
    }

    // MARK: - 页面

    private func content(_ endpoint: API.EndpointView) -> some View {
        Form {
            if let error = store.error {
                Section {
                    SettingsBNotice(text: error, tone: .danger).accessibilityIdentifier("mcp-error")
                }
            }
            header(endpoint)
            Section {
                Picker("视图", selection: $tab) {
                    ForEach(Tab.allCases) { item in
                        Text(item == .tools ? "工具 \(endpoint.toolCount)" : item.rawValue).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets())
                .accessibilityIdentifier("mcp-detail-tabs")
            }
            switch tab {
            case .overview: overview(endpoint)
            case .tools:
                if let preview {
                    SettingsBMCPToolCatalog(tools: preview.tools)
                } else {
                    Section { Text("正在计算工具目录…").foregroundStyle(Theme.textMuted) }
                }
            case .settings: settings(endpoint)
            }
        }
        .settingsBFormStyle()
        .refreshable { await store.load(api) }
    }

    /// 头部：名字 + 启停开关，下一行淡色元信息（状态 / 地址 / 形态）
    private func header(_ endpoint: API.EndpointView) -> some View {
        Section {
            Toggle(isOn: Binding(
                get: { endpoint.enabled },
                set: { next in
                    guard !store.busy else { return }
                    Task { _ = await store.run(api) { try await api.mcpEndpointsUpdate(endpointId: endpoint.id, body: .init(enabled: next)) } }
                }
            )) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(endpoint.name).font(.title3.weight(.medium)).lineLimit(1)
                    HStack(spacing: 6) {
                        SettingsBMCPStatusDot(on: endpoint.enabled)
                        Text(endpoint.enabled ? "运行中" : "已停用")
                        Text("·").foregroundStyle(Theme.textFaint)
                        Text("/mcp/\(endpoint.slug)").monospaced()
                        Text("·").foregroundStyle(Theme.textFaint)
                        Text(SettingsBMCPFormat.mode(endpoint.expandTools))
                    }
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                }
            }
            .disabled(store.busy)
            .accessibilityLabel("启用 \(endpoint.name)")
            .accessibilityIdentifier("mcp-endpoint-enabled")
        }
    }

    // MARK: - 概览

    @ViewBuilder
    private func overview(_ endpoint: API.EndpointView) -> some View {
        // 接进一个客户端要填的全部东西，就是地址 + 认证头
        Section {
            SettingsBMCPCopyField(value: store.fullURL(endpoint), label: "复制地址", identifier: "mcp-detail-copy-url")
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets())
            if !endpoint.url.hasPrefix("http") {
                SettingsBNotice(text: "还没配置外部访问地址，这里只有相对路径。外部客户端要连上，先去「设置 → 网络」填对外地址。", tone: .warn)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets())
            }
        } header: {
            Text("端点地址")
        }

        Section {
            SettingsBMCPCodeBlock(code: "Authorization: Bearer <你的端点令牌>", lang: "http", identifier: "mcp-detail-copy-header")
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets())
        } header: {
            Text("认证请求头")
        } footer: {
            Text("令牌明文只在创建和轮换时显示一次（服务端只存哈希），忘了就在下面轮换一枚新的。传输是 Streamable HTTP，客户端里选「HTTP」而不是 SSE；claude.ai 网页版的自定义连接器只支持 OAuth，暂时接不进来，Claude Code、Cursor、Cline 等本地客户端都可以。")
        }

        selfCheckSection(endpoint)

        Section {
            VStack(alignment: .leading, spacing: 6) {
                Text("服务").foregroundStyle(Theme.textMuted)
                // 详情页不设上限：这一屏就是要看全「到底开放了什么」
                SettingsBMCPServiceChips(services: endpoint.services, max: endpoint.services.count)
            }
            LabeledContent("工具") {
                Text(preview.map { "定义约 \(SettingsBMCPFormat.bytes($0.approxBytes))" } ?? "计算中…")
            }
            LabeledContent("令牌") {
                HStack(spacing: 8) {
                    Text(endpoint.tokenHint).monospaced()
                    Button("轮换") { Task { await rotate(endpoint) } }
                        .font(.caption.weight(.medium))
                        .buttonStyle(.glass)
                        .disabled(store.busy)
                        .accessibilityIdentifier("mcp-detail-rotate")
                }
            }
            LabeledContent("最近调用") {
                Text(endpoint.lastUsedAt == nil ? "从未调用" : SettingsBMCPFormat.relative(endpoint.lastUsedAt))
                    .accessibilityIdentifier("mcp-detail-last-used")
            }
        }

        if !endpoint.missingServices.isEmpty {
            Section {
                SettingsBNotice(
                    text: "配置里有 \(endpoint.missingServices.count) 个服务在当前版本已不存在，已忽略：\(endpoint.missingServices.joined(separator: "、"))",
                    tone: .warn
                )
            }
        }
    }

    /// 连通性自检：跑一遍真实协议，只试调只读工具，就地回答「它现在能用吗」
    private func selfCheckSection(_ endpoint: API.EndpointView) -> some View {
        Section {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("连通性自检").font(.subheadline.weight(.medium))
                    Text("跑一遍真实协议，只试调只读工具，不改任何状态。")
                        .font(.caption).foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                Button(checking ? "自检中…" : check == nil ? "运行自检" : "重新自检") {
                    Task { await runCheck(endpoint) }
                }
                .font(.subheadline.weight(.medium))
                .buttonStyle(.glass)
                .disabled(checking)
                .accessibilityIdentifier("mcp-detail-check")
            }
            if let check {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        SettingsBMCPStatusDot(on: check.ok)
                        Text(check.message)
                            .font(.subheadline)
                            .foregroundStyle(check.ok ? Theme.text : Theme.danger)
                            .fixedSize(horizontal: false, vertical: true)
                        Text("\(check.elapsedMs) ms").font(.caption.monospacedDigit()).foregroundStyle(Theme.textFaint)
                    }
                    Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
                        GridRow {
                            Text("协议").foregroundStyle(Theme.textFaint)
                            Text(check.protocolVersion.isEmpty ? "—" : check.protocolVersion).monospaced()
                        }
                        GridRow {
                            Text("工具").foregroundStyle(Theme.textFaint)
                            Text("\(check.toolCount) 个").monospacedDigit()
                        }
                        if !check.probeTool.isEmpty {
                            GridRow(alignment: .firstTextBaseline) {
                                Text("试调").foregroundStyle(Theme.textFaint)
                                Text("\(Text(check.probeTool).monospaced().foregroundStyle(Theme.accent))  \(Text(check.probeOk ? "成功" : "失败").foregroundStyle(check.probeOk ? Theme.text : Theme.danger))  \(Text(String(check.probeMessage.prefix(60))).foregroundStyle(Theme.textFaint))")
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .font(.caption)
                    ForEach(check.warnings, id: \.self) { warning in
                        SettingsBNotice(text: warning, tone: .warn)
                    }
                }
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("mcp-detail-check-result")
            }
        }
    }

    // MARK: - 设置

    @ViewBuilder
    private func settings(_ endpoint: API.EndpointView) -> some View {
        let binding = Binding(
            get: { draft ?? SettingsBMCPDraft(endpoint) },
            set: { draft = $0 }
        )
        let current = binding.wrappedValue
        SettingsBMCPEndpointFields(
            draft: binding,
            services: store.status?.services ?? [],
            baseUrl: store.status?.baseUrl ?? "",
            slugEditable: false
        )
        Section {
            HStack(spacing: 10) {
                Button {
                    dismiss()
                } label: {
                    Text("取消").frame(maxWidth: .infinity)
                }
                .buttonStyle(.glass)
                .accessibilityIdentifier("mcp-form-cancel")
                Button {
                    Task { await save(endpoint, current) }
                } label: {
                    Text("保存修改").font(.body.weight(.semibold)).frame(maxWidth: .infinity)
                }
                .discoverProminentButton()
                .disabled(store.busy || current.name.trimmingCharacters(in: .whitespaces).isEmpty || current.services.isEmpty)
                .accessibilityIdentifier("mcp-form-submit")
            }
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets())
        }

        // 危险区：沉到最底，删除要打字确认——端点一删，接入它的客户端立刻全断，且不可恢复
        Section {
            Text("删除后地址与令牌一并作废，不可恢复；已接入的客户端会立刻失败。确认请输入端点标识 \(Text(endpoint.slug).monospaced().foregroundStyle(Theme.text))。")
                .font(.caption)
                .foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            TextField(endpoint.slug, text: $confirmText)
                .font(.body.monospaced())
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .accessibilityIdentifier("mcp-delete-confirm")
            Button(role: .destructive) {
                Task { await remove(endpoint) }
            } label: {
                Text("删除这个端点").frame(maxWidth: .infinity)
            }
            .disabled(store.busy || confirmText != endpoint.slug)
            .accessibilityIdentifier("mcp-delete")
        } header: {
            Text("危险操作").foregroundStyle(Theme.danger)
        }
    }

    // MARK: - 动作

    private func loadPreview(_ endpoint: API.EndpointView) async {
        preview = nil
        preview = try? await api.mcpEndpointsPreview(body: .init(services: endpoint.services, expandTools: endpoint.expandTools))
    }

    /// 自检请求本身失败（网络断、接口不存在）也显示成一次「未通过」——自检的意义就是给出结论
    private func runCheck(_ endpoint: API.EndpointView) async {
        checking = true
        defer { checking = false }
        do {
            check = try await api.mcpEndpointsCheck(endpointId: endpoint.id)
        } catch {
            check = API.SelfCheckView(
                ok: false, message: "自检请求失败：\(error.localizedDescription)", protocolVersion: "",
                toolCount: 0, elapsedMs: 0, probeTool: "", probeOk: false, probeMessage: "", warnings: []
            )
        }
    }

    private func rotate(_ endpoint: API.EndpointView) async {
        guard await feedback.confirm(
            "轮换令牌？",
            message: "会生成一枚新令牌，旧令牌立即失效。已接入的客户端都要更新配置，否则会开始报 401。",
            confirmTitle: "生成新令牌"
        ) else { return }
        if let rotated = await store.run(api, { try await api.mcpEndpointsRotateToken(endpointId: endpoint.id) }) {
            issued = SettingsBMCPIssued(created: rotated)
        }
    }

    private func save(_ endpoint: API.EndpointView, _ draft: SettingsBMCPDraft) async {
        let body = API.EndpointUpdateRequest(
            name: draft.name,
            services: draft.services,
            expandTools: draft.expandTools,
            timeoutSeconds: draft.timeoutSeconds
        )
        _ = await store.run(api) { try await api.mcpEndpointsUpdate(endpointId: endpoint.id, body: body) }
    }

    private func remove(_ endpoint: API.EndpointView) async {
        if await store.run(api, { try await api.mcpEndpointsDelete(endpointId: endpoint.id) }) != nil {
            dismiss()
        }
    }
}

/// 令牌专屏的弹出参数（不给生成模型加 Identifiable 一致性，包一层）
struct SettingsBMCPIssued: Identifiable {
    let id = UUID()
    let created: API.EndpointCreatedView
}
