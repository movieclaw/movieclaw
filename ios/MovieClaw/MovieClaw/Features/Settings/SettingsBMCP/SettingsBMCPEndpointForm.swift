import SwiftUI

/// 端点表单草稿（新建与编辑共用；数字以字符串暂存）
struct SettingsBMCPDraft: Equatable {
    var name = ""
    var slug = ""
    var services: [String] = []
    var description: String?
    var expandTools = true
    var timeout = "300"

    init() {}

    init(_ endpoint: API.EndpointView) {
        name = endpoint.name
        slug = endpoint.slug
        services = endpoint.services
        description = endpoint.description
        expandTools = endpoint.expandTools
        timeout = String(endpoint.timeoutSeconds)
    }

    var timeoutSeconds: Int? { Int(timeout.trimmingCharacters(in: .whitespaces)) }
}

/// 新建 / 编辑端点的字段区（对应 Web `EndpointForm`），产出若干 `Section`，嵌进调用方的 `Form`。
///
/// 按「配置 ↔ 后果」重排：
/// - 名称与地址标识（新建时即时校验格式与重名，不等提交换回 409）；
/// - 工具形态做成两张对比卡（展开 / 折叠），差异（工具数、体积、调用样式）直接写在卡面；
/// - 服务选择器可搜索、已选置顶，每条两行（域名 + 说明），说明是判断该不该勾的唯一依据，不截断；
/// - 「客户端将看到」：实时预览这套配置真实产出的工具面（`POST /mcp/endpoints/preview`，纯内存试算）。
///   Web 桌面放右栏，手机上顺延到最后一个 Section。
struct SettingsBMCPEndpointFields: View {
    @Binding var draft: SettingsBMCPDraft
    let services: [API.ServiceView]
    let baseUrl: String
    let slugEditable: Bool
    var takenSlugs: [String] = []

    @Environment(\.api) private var api
    @State private var query = ""
    /// 预览工具面；nil = 计算中
    @State private var preview: [API.ToolPreview]?

    /// 展开模式超过这个数就建议改折叠（业界观察到的模型退化下沿）。只建议，不拦
    private static let toolHintThreshold = 30
    /// 与后端 SLUG_PATTERN 同口径：小写字母数字与连字符，首尾不为连字符
    private static let slugPattern = #"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$"#

    /// 地址标识的即时校验：空值不报错（还没开始填），有值才判
    static func slugError(_ slug: String, editable: Bool, taken: [String]) -> String? {
        let s = slug.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty, editable else { return nil }
        if s.range(of: slugPattern, options: .regularExpression) == nil {
            return "只能用小写字母、数字和连字符，且不能以连字符开头或结尾"
        }
        if taken.contains(s) { return "这个标识已被其他端点占用" }
        return nil
    }

    private var picked: Set<String> { Set(draft.services) }
    private var chosen: [API.ServiceView] { services.filter { picked.contains($0.domain) } }
    private var commandTotal: Int { chosen.reduce(0) { $0 + $1.commandCount } }
    private var expandedBytes: Int { chosen.reduce(0) { $0 + $1.expandedBytes } }
    private var collapsedBytes: Int { chosen.reduce(0) { $0 + $1.collapsedBytes } }
    private var toolCount: Int { draft.expandTools ? commandTotal : chosen.count }
    private var bytes: Int { draft.expandTools ? expandedBytes : collapsedBytes }

    /// 搜索过滤 + 已选置顶：滚到哪儿都看得见自己选了什么
    private var visible: [API.ServiceView] {
        let keyword = query.trimmingCharacters(in: .whitespaces).lowercased()
        let rows = keyword.isEmpty ? services : services.filter {
            $0.domain.lowercased().contains(keyword) || $0.description.lowercased().contains(keyword)
        }
        return rows.filter { picked.contains($0.domain) } + rows.filter { !picked.contains($0.domain) }
    }

    private struct SettingsBMCPPreviewKey: Equatable {
        var services: [String]
        var expand: Bool
    }

    var body: some View {
        basicSection
        modeSection
        servicesSection
        Section {
            SettingsBNumberField(label: "单次调用超时（秒）", text: $draft.timeout, placeholder: "300", identifier: "mcp-form-timeout")
        }
        previewSection
    }

    // MARK: 名称与地址标识

    private var basicSection: some View {
        let slugError = Self.slugError(draft.slug, editable: slugEditable, taken: takenSlugs)
        return Section {
            SettingsBTextField(label: "端点名称", text: $draft.name, placeholder: "家庭影音助理", identifier: "mcp-form-name")
            VStack(alignment: .leading, spacing: 6) {
                Text("地址标识" + (slugEditable ? "" : "（建成后不可改）"))
                    .font(.subheadline).foregroundStyle(Theme.textMuted)
                TextField("home-assistant", text: $draft.slug)
                    .font(.body.monospaced())
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .disabled(!slugEditable)
                    .opacity(slugEditable ? 1 : 0.5)
                    .accessibilityIdentifier("mcp-form-slug")
            }
            .padding(.vertical, 2)
        } footer: {
            if let slugError {
                Text(slugError).foregroundStyle(Theme.danger).accessibilityIdentifier("mcp-form-slug-error")
            } else {
                Text("\(baseUrl)/mcp/\(draft.slug.isEmpty ? "<地址标识>" : draft.slug)").font(.caption.monospaced())
            }
        }
    }

    // MARK: 工具形态

    private var modeSection: some View {
        Section("工具形态") {
            modeCard(expand: true, title: "展开", sample: "subscriptions_update",
                     desc: "一条命令一个工具，参数带类型，模型照 schema 填",
                     count: commandTotal, size: expandedBytes)
            modeCard(expand: false, title: "折叠", sample: "subscriptions(command, params)",
                     desc: "一个服务一个工具，工具少、占的上下文小",
                     count: chosen.count, size: collapsedBytes)
        }
    }

    private func modeCard(expand: Bool, title: String, sample: String, desc: String, count: Int, size: Int) -> some View {
        let on = draft.expandTools == expand
        return Button {
            draft.expandTools = expand
        } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: on ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(on ? Theme.accentStrong : Theme.textFaint)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(title).font(.subheadline.weight(.medium)).foregroundStyle(Theme.text)
                        Spacer()
                        Text(chosen.isEmpty ? "—" : "\(count) 个工具 · \(SettingsBMCPFormat.bytes(size))")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(Theme.textMuted)
                    }
                    Text(sample).font(.system(size: 11).monospaced()).foregroundStyle(Theme.accent)
                    Text(desc).font(.caption).foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .listRowBackground(on ? Theme.accentSoft : nil)
        .accessibilityIdentifier(expand ? "mcp-form-mode-expand" : "mcp-form-mode-collapse")
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    // MARK: 服务选择器

    private var servicesSection: some View {
        Section {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(Theme.textFaint)
                TextField("搜索服务…", text: $query)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityIdentifier("mcp-form-service-search")
                if !draft.services.isEmpty {
                    Button("清空") { draft.services = [] }
                        .font(.caption)
                        .buttonStyle(.glass)
                        .accessibilityIdentifier("mcp-form-service-clear")
                }
            }
            ForEach(visible, id: \.domain) { service in
                let on = picked.contains(service.domain)
                Button {
                    if on {
                        draft.services.removeAll { $0 == service.domain }
                    } else {
                        draft.services.append(service.domain)
                    }
                } label: {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: on ? "checkmark.square.fill" : "square")
                            .foregroundStyle(on ? Theme.accentStrong : Theme.textFaint)
                            .padding(.top, 1)
                        // 两行：域名与说明各一行。说明是判断该不该勾的依据，不能截断
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 8) {
                                Text(service.domain).font(.subheadline.monospaced()).foregroundStyle(Theme.text)
                                Text("\(service.commandCount) 条命令").font(.caption.monospacedDigit()).foregroundStyle(Theme.textFaint)
                            }
                            Text(service.description)
                                .font(.caption)
                                .foregroundStyle(Theme.textMuted)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .listRowBackground(on ? Theme.accentSoft : nil)
                .accessibilityIdentifier("mcp-form-service-\(service.domain)")
                .accessibilityAddTraits(on ? .isSelected : [])
            }
        } header: {
            Text("开放的服务 · 已选 \(chosen.count) / \(services.count)")
        } footer: {
            Text("勾中的服务，接进来的模型就能全部执行，和你自己在命令行上能做的一样——勾了「library」它就可以删除磁盘上的媒体文件。")
        }
    }

    // MARK: 实时后果

    private var previewSection: some View {
        Section("客户端将看到") {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text("\(toolCount)").font(.title.weight(.medium).monospacedDigit())
                    Text("个工具").foregroundStyle(Theme.textMuted)
                }
                Text("\(chosen.count) 个服务 · 覆盖 \(commandTotal) 条命令 · 定义约 \(SettingsBMCPFormat.bytes(bytes))")
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("mcp-form-totals")
            // 任务挂在具体行上而不是 Section：List 会把 Section 上的修饰符分发到每一行，变成多份任务
            .task(id: SettingsBMCPPreviewKey(services: draft.services, expand: draft.expandTools)) {
                await loadPreview()
            }

            if draft.expandTools && toolCount > Self.toolHintThreshold {
                SettingsBNotice(
                    text: "超过 \(Self.toolHintThreshold) 个工具后模型选择准确率会下降。改用「折叠」可降到 \(chosen.count) 个工具、约 \(SettingsBMCPFormat.bytes(collapsedBytes))。",
                    tone: .warn
                )
            }

            if let preview {
                if preview.isEmpty {
                    Text("选中服务后这里会列出工具").font(.caption).foregroundStyle(Theme.textFaint)
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(preview.prefix(60), id: \.name) { tool in
                            Text(tool.name)
                                .font(.caption.monospaced())
                                .foregroundStyle(Theme.accent)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                        if preview.count > 60 {
                            Text("…… 还有 \(preview.count - 60) 个").font(.caption).foregroundStyle(Theme.textFaint)
                        }
                    }
                }
            } else {
                Text("正在计算…").font(.caption).foregroundStyle(Theme.textFaint)
            }
        }
    }

    /// 服务或模式一变就重算（请求很轻，纯内存渲染；`.task(id:)` 自动取消上一轮）
    private func loadPreview() async {
        if draft.services.isEmpty {
            preview = []
            return
        }
        preview = nil
        if let result = try? await api.mcpEndpointsPreview(body: .init(services: draft.services, expandTools: draft.expandTools)) {
            preview = result.tools
        }
    }
}

// MARK: - 新建端点弹层

/// 新建端点（Web `view.creating` 视图）：字段区 + 底栏「创建端点」。
/// 创建成功后弹层原地换成令牌专屏（令牌明文只在这一次响应里），确认保存后关闭并交给列表页推入详情。
struct SettingsBMCPCreateSheet: View {
    let store: SettingsBMCPStore
    /// 用户在令牌专屏点「我已保存」后回调端点 id，由列表页推入详情
    let onFinished: (String) -> Void

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss
    @State private var draft = SettingsBMCPDraft()
    @State private var issued: API.EndpointCreatedView?

    private var takenSlugs: [String] { store.status?.endpoints.map(\.slug) ?? [] }

    private var canSubmit: Bool {
        !store.busy
            && !draft.name.trimmingCharacters(in: .whitespaces).isEmpty
            && !draft.slug.trimmingCharacters(in: .whitespaces).isEmpty
            && !draft.services.isEmpty
            && SettingsBMCPEndpointFields.slugError(draft.slug, editable: true, taken: takenSlugs) == nil
    }

    var body: some View {
        if let issued {
            SettingsBMCPTokenIssuedView(
                name: issued.endpoint.name,
                url: store.fullURL(issued.endpoint),
                token: issued.token
            ) {
                onFinished(issued.endpoint.id)
                dismiss()
            }
        } else {
            form
        }
    }

    private var form: some View {
        NavigationStack {
            Form {
                if let error = store.error {
                    Section {
                        SettingsBNotice(text: error, tone: .danger).accessibilityIdentifier("mcp-error")
                    }
                }
                SettingsBMCPEndpointFields(
                    draft: $draft,
                    services: store.status?.services ?? [],
                    baseUrl: store.status?.baseUrl ?? "",
                    slugEditable: true,
                    takenSlugs: takenSlugs
                )
            }
            .scrollContentBackground(.hidden)
            .navigationTitle("创建端点")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消", systemImage: "xmark") { dismiss() }
                        .accessibilityIdentifier("mcp-form-cancel")
                }
            }
            .safeAreaBar(edge: .bottom) {
                SubsPrimaryButton(title: "创建端点", busy: store.busy, enabled: canSubmit, identifier: "mcp-form-submit") {
                    Task { await submit() }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
            }
            .background(Theme.background.opacity(0.35))
        }
        .presentationBackground(.regularMaterial)
        .interactiveDismissDisabled(store.busy)
    }

    private func submit() async {
        let body = API.EndpointCreateRequest(
            name: draft.name.trimmingCharacters(in: .whitespaces),
            slug: draft.slug.trimmingCharacters(in: .whitespaces),
            services: draft.services,
            description: nil,
            expandTools: draft.expandTools,
            timeoutSeconds: draft.timeoutSeconds
        )
        if let created = await store.run(api, { try await api.mcpEndpointsCreate(body: body) }) {
            issued = created
        }
    }
}
